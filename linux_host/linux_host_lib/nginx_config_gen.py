import subprocess
import sys
from pathlib import Path
from typing import Any

from linux_host.linux_host_lib.linux_host_config import get_linux_host_config
from linux_host.linux_host_lib.metadata_to_tilejson import write_tilejson
from linux_host.linux_host_lib.telegram_alerts import send_telegram_alert
from linux_host.linux_host_lib.tile_auth import (
    cors_preflight,
    secure_link_guard,
    secure_link_server_directives,
    write_secure_link_map,
)


HTTP_REDIRECT_SERVER = """server {
    listen 80;
    listen [::]:80;
    server_name __DOMAIN_SLUG__ __DOMAIN__;

    # ACME HTTP-01 challenge requests are intercepted by ngx_http_acme_module
    # before normal location processing, so regular HTTP traffic can redirect.
    return 308 https://$host$request_uri;
}"""

# Mozilla Guideline v6.0 intermediate config for nginx + OpenSSL 3.x.
# 3.0.2 and 3.0.13 currently generate the same config.
# Do not use the OpenSSL 4.0 X25519MLKEM768 variant yet: current Ubuntu 24.04
# servers with OpenSSL 3.0 reject it in nginx -t.
# https://ssl-config.mozilla.org/#server=nginx&version=1.27.3&config=intermediate&openssl=3.0.2&guideline=6.0
SSL_INTERMEDIATE_CONFIG = """# intermediate configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ecdh_curve X25519:prime256v1:secp384r1;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;

    ssl_session_timeout 1d;
    ssl_session_cache shared:MozSSL:10m; # about 40000 sessions"""

# Do not assume the operator controls every subdomain, and do not preload.
NOINDEX_HEADERS = """add_header X-Robots-Tag "noindex, nofollow" always;
add_header Strict-Transport-Security "max-age=63072000" always;"""

PUBLIC_HEADERS = f"""add_header 'Access-Control-Allow-Origin' '*' always;
add_header Cache-Control public;
{NOINDEX_HEADERS}"""


def write_nginx_config_if_changed(
    retained_versions: dict[str, set[str]], active_versions: dict[str, str]
) -> None:
    print('Writing nginx config')
    get_linux_host_config().mnt_dir.mkdir(parents=True, exist_ok=True)

    # (Re)write the http-level kid->secret map before validating/reloading nginx, so the
    # $ofm_secret variable referenced by the server-level secure_link_md5 directive exists.
    # It lives in /data/nginx/config/ (included at http level) rather than in sites/, so its
    # own change must be tracked separately to decide whether a reload is needed.
    map_changed = write_secure_link_map()

    domains = get_linux_host_config().domains
    # When every domain is served plain-HTTP behind an ALB, the first ALB vhost becomes the
    # port-80 default_server so it also answers ALB health checks (Host = target IP). In that
    # mode the deploy does not install default_disable, so there is no default_server clash.
    alb_mode = all(domain_data['cert']['type'] == 'alb' for domain_data in domains)
    desired = {
        f'ofm-{domain_data["slug"]}.conf': create_domain_config(
            domain_data, retained_versions, active_versions, is_alb_default=(alb_mode and i == 0)
        )
        for i, domain_data in enumerate(domains)
    }
    existing_files = list(get_linux_host_config().nginx_sites_dir.glob('ofm-*.conf'))
    existing = {path.name: path.read_text() for path in existing_files}
    changed = desired != existing or map_changed
    if changed:
        for filename, content in desired.items():
            (get_linux_host_config().nginx_sites_dir / filename).write_text(content)
        for path in existing_files:
            if path.name not in desired:
                path.unlink()
    else:
        print('nginx config unchanged')

    # Always validate saved files, but reload only for generated changes. Keeping
    # no persistent reload marker makes stable minutely syncs stateless.
    result = subprocess.run(['nginx', '-t'])
    if result.returncode != 0:
        send_telegram_alert('ERROR\nnginx config test failed')
        result.check_returncode()
    if changed:
        _reload_nginx_if_running()


def _reload_nginx_if_running() -> None:
    """Reload nginx to pick up a config change, but only if it is already running.

    At boot ``ofm-tile-auth.service`` regenerates the config while ordered ``Before=nginx.service``,
    so nginx is still inactive. ``systemctl reload nginx`` on an inactive unit escalates to a
    *start*, whose job is queued behind this oneshot finishing -- a circular wait that hangs the
    whole boot (the unit blocks on the reload; the reload blocks on nginx starting, which blocks
    on the unit). Since nginx is ordered after us it reads the freshly written config on its own
    start, so no reload is needed then. A reload only makes sense on the live-rotation / minutely
    sync path, when nginx is already up; there the config was already validated by ``nginx -t``
    above, so a reload failure is logged but never made fatal.
    """
    is_active = subprocess.run(['systemctl', 'is-active', '--quiet', 'nginx'])
    if is_active.returncode != 0:
        print('nginx is not running; skipping reload (it will load the new config on start)')
        return
    if subprocess.run(['systemctl', 'reload', 'nginx']).returncode != 0:
        print('warning: nginx reload failed; the validated new config is in place on disk')


def create_domain_config(
    domain_data: dict[str, Any],
    retained_versions: dict[str, set[str]],
    active_versions: dict[str, str],
    is_alb_default: bool = False,
) -> str:
    cert_type = domain_data['cert']['type']
    if cert_type == 'upload':
        cert_file = Path(f'/data/nginx/certs/ofm-{domain_data["slug"]}.cert')
        key_file = Path(f'/data/nginx/certs/ofm-{domain_data["slug"]}.key')

        if not cert_file.is_file() or not key_file.is_file():
            sys.exit(f'  cert or key file does not exist: {cert_file} {key_file}')

    return create_nginx_conf(domain_data, retained_versions, active_versions, is_alb_default)


def create_nginx_conf(
    domain_data: dict[str, Any],
    retained_versions: dict[str, set[str]],
    active_versions: dict[str, str],
    is_alb_default: bool = False,
) -> str:
    dynamic_block_text = dynamic_blocks(domain_data, retained_versions, active_versions)

    # 'alb': TLS is terminated upstream (e.g. an AWS ALB), so nginx serves plain HTTP on
    # port 80 only, with no certificate, no ACME issuer and no HTTP->HTTPS redirect.
    if domain_data['cert']['type'] == 'alb':
        template_name = 'common_alb.conf'
    else:
        template_name = 'common.conf'
    template = (get_linux_host_config().nginx_templates_dir / template_name).read_text()

    # Only the first ALB vhost claims the port-80 default_server (see write_nginx_config_if_changed).
    template = template.replace('__ALB_DEFAULT__', ' default_server' if is_alb_default else '')
    template = template.replace('__DYNAMIC_BLOCKS__', dynamic_block_text)
    template = template.replace('__ACME_ISSUER__', acme_issuer(domain_data))
    template = template.replace('__HTTP_REDIRECT_SERVER__', HTTP_REDIRECT_SERVER)
    template = template.replace('__SSL_INTERMEDIATE_CONFIG__', SSL_INTERMEDIATE_CONFIG)
    template = template.replace('__NOINDEX_HEADERS__', NOINDEX_HEADERS)
    template = template.replace('__PUBLIC_HEADERS__', PUBLIC_HEADERS)
    template = template.replace(
        '    __SSL_CERTIFICATE_DIRECTIVES__', ssl_certificate_directives(domain_data)
    )

    # Fill the secure_link placeholders last so they also reach the dynamic location blocks
    # (both the generated ones and the static blocks); empty strings when auth is disabled.
    template = template.replace('__SECURE_LINK_SERVER__', secure_link_server_directives())
    # The CORS preflight is always emitted (so OPTIONS returns 204 instead of 405 on the
    # static-file locations); the secure_link token guard is only added when auth is enabled.
    template = template.replace('__SECURE_LINK_GUARD__', cors_preflight() + secure_link_guard())

    template = template.replace('__DOMAIN_SLUG__', domain_data['slug'])
    template = template.replace('__DOMAIN__', domain_data['domain'])

    print(f'  nginx config generated: {domain_data["domain"]} {domain_data["slug"]}')
    return template


def acme_issuer(domain_data: dict[str, Any]) -> str:
    if domain_data['cert']['type'] != 'letsencrypt':
        return ''

    return f"""acme_issuer ofm_{domain_data['slug']} {{
    uri https://acme-v02.api.letsencrypt.org/directory;
    contact mailto:{domain_data['cert']['email']};
    state_path /data/nginx/acme/{domain_data['slug']};
    accept_terms_of_service;
}}"""


def ssl_certificate_directives(domain_data: dict[str, Any]) -> str:
    cert_type = domain_data['cert']['type']
    if cert_type == 'alb':
        # No TLS in nginx; certificates are handled upstream (ALB/proxy).
        return ''

    if cert_type == 'upload':
        return f"""    ssl_certificate /data/nginx/certs/ofm-{domain_data['slug']}.cert;
    ssl_certificate_key /data/nginx/certs/ofm-{domain_data['slug']}.key;"""

    if cert_type == 'dummy':
        return """    ssl_certificate /etc/nginx/ssl/self_signed.cert;
    ssl_certificate_key /etc/nginx/ssl/self_signed.key;"""

    if cert_type == 'letsencrypt':
        return f"""    acme_certificate ofm_{domain_data['slug']} {domain_data['domain']} key=ecdsa:256;
    ssl_certificate $acme_certificate;
    ssl_certificate_key $acme_certificate_key;
    ssl_certificate_cache max=10 inactive=1h valid=10m;"""

    raise ValueError(f'Unknown certificate type: {cert_type}')


def dynamic_blocks(
    domain_data: dict[str, Any],
    retained_versions: dict[str, set[str]],
    active_versions: dict[str, str],
) -> str:
    nginx_conf_text = ''

    for area, versions in retained_versions.items():
        for version in sorted(versions):
            mnt_dir = get_linux_host_config().mnt_dir / f'{area}-{version}'
            nginx_conf_text += create_version_location(
                area=area, version=version, mnt_dir=mnt_dir, domain_data=domain_data
            )

    nginx_conf_text += create_latest_locations(
        domain_data=domain_data, active_versions=active_versions
    )

    static_blocks = (get_linux_host_config().nginx_templates_dir / 'static_blocks.conf').read_text()
    static_blocks = static_blocks.replace('__ROOT_REDIRECT_BLOCK__', root_redirect_block())
    nginx_conf_text += '\n' + static_blocks
    return nginx_conf_text


def root_redirect_block() -> str:
    if get_linux_host_config().root_redirect_url:
        return f"""location = / {{
    return 302 {get_linux_host_config().root_redirect_url};
}}
"""

    return """location = / {
    default_type text/plain;
    return 200 'This is an OpenFreeMap tile server.\nhttps://openfreemap.org\n';
}
"""


def create_version_location(
    *, area: str, version: str, mnt_dir: Path, domain_data: dict[str, Any]
) -> str:
    run_dir = get_linux_host_config().versions_dir / area / version
    if not run_dir.is_dir():
        print(f"  {run_dir} doesn't exist, skipping")
        return ''

    tilejson_path = run_dir / f'tilejson-{domain_data["slug"]}.json'

    metadata_path = mnt_dir / 'metadata.json'
    if not metadata_path.is_file():
        print(f"  {metadata_path} doesn't exist, skipping")
        return ''

    url_prefix = f'https://{domain_data["domain"]}/{area}/{version}'

    if not tilejson_path.exists():
        write_tilejson(metadata_path, tilejson_path, url_prefix)

    return f"""
    # specific JSON {area} {version}
    location = /{area}/{version} {{ # no trailing slash
        alias {tilejson_path}; # no trailing slash
__SECURE_LINK_GUARD__
        expires 1w;
        default_type application/json;

        {PUBLIC_HEADERS}

        add_header x-ofm-debug 'specific JSON {area} {version}';
    }}

    # specific PBF {area} {version}
    location ^~ /{area}/{version}/ {{ # trailing slash
        alias {mnt_dir}/tiles/; # trailing slash
__SECURE_LINK_GUARD__
        try_files $uri @empty_tile;
        add_header Content-Encoding gzip;

        expires 10y;

        types {{
            application/vnd.mapbox-vector-tile pbf;
        }}

        {PUBLIC_HEADERS}

        add_header x-ofm-debug 'specific PBF {area} {version}';
    }}
    """


def create_latest_locations(*, domain_data: dict[str, Any], active_versions: dict[str, str]) -> str:
    location_str = ''

    for area, version in active_versions.items():
        print(f'  linking latest version for {area}: {version}')

        run_dir = get_linux_host_config().versions_dir / area / version
        tilejson_path = run_dir / f'tilejson-{domain_data["slug"]}.json'
        if not tilejson_path.is_file():
            print(
                f'    skipping latest block for {area} / {version}: {tilejson_path} does not exist'
            )
            continue

        # checking mnt dir
        mnt_dir = Path(f'/mnt/ofm/{area}-{version}')
        mnt_file = mnt_dir / 'metadata.json'
        if not mnt_file.is_file():
            print(f'    skipping latest block for {area} / {version}: {mnt_file} does not exist')
            continue

        # latest
        location_str += f"""

        # latest JSON {area}
        location = /{area} {{ # no trailing slash
            alias {tilejson_path}; # no trailing slash
__SECURE_LINK_GUARD__
            expires 1d;
            default_type application/json;

            {PUBLIC_HEADERS}

            add_header x-ofm-debug 'latest JSON {area}';
        }}
        """

        # Missing version URLs intentionally fall back to the active version.
        # This bounded-storage policy accepts mixed responses from shared caches.
        # wildcard
        # identical to create_version_location
        location_str += f"""

        # wildcard JSON {area}
        location ~ ^/{area}/([^/]+)$ {{
            # regex location is unreliable with alias, only root is reliable

            root {run_dir}; # no trailing slash
__SECURE_LINK_GUARD__
            try_files /tilejson-{domain_data['slug']}.json =404;

            expires 1w;
            default_type application/json;

            {PUBLIC_HEADERS}

            add_header x-ofm-debug 'wildcard JSON {area}';
        }}

        # wildcard PBF {area}
        location ~ ^/{area}/([^/]+)/(.+)$ {{
            # regex location is unreliable with alias, only root is reliable

            root {mnt_dir}/tiles/; # trailing slash
__SECURE_LINK_GUARD__
            try_files /$2 @empty_tile;
            add_header Content-Encoding gzip;

            expires 10y;

            types {{
                application/vnd.mapbox-vector-tile pbf;
            }}

            {PUBLIC_HEADERS}

            add_header x-ofm-debug 'wildcard PBF {area}';
        }}

        # health probe {area}
        # A GET to /healthz/{area} returns 204 (empty body) iff the btrfs image
        # for this run is mounted, and 503 otherwise. The discriminator is the
        # tiles/ directory: the mountpoint dir /mnt/ofm/{area}-{version} always
        # exists, but tiles/ only appears when the image is mounted. This gives an
        # ALB a truthful signal instead of the masked 200 that @empty_tile returns
        # for real tile requests once a mount drops. `if` here only guards a
        # `return`, which is a sanctioned use of if in a location context. The
        # probe is intentionally NOT behind the secure_link guard.
        location = /healthz/{area} {{
            access_log off;
            if (!-d {mnt_dir}/tiles) {{
                return 503;
            }}
            add_header x-ofm-debug 'healthz {area}' always;
            return 204;
        }}
        """

    return location_str


def regenerate_nginx_from_local_state() -> None:
    """Rewrite the nginx config from the versions already present locally, and reload.

    Used by the boot-time tile-auth service and the live rotation command to (re)apply the
    current tile-auth secrets without running a full sync/download. The btrfs images are
    already mounted at this point (fstab / a prior sync), so the deployed pointer files under
    state/ are enough to rebuild the "latest"/wildcard location blocks.
    """
    from linux_host.linux_host_lib.versions import get_local_deployed_versions

    active_versions = get_local_deployed_versions()
    retained_versions = {area: {version} for area, version in active_versions.items()}
    write_nginx_config_if_changed(retained_versions, active_versions)
