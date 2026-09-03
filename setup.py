from setuptools import find_packages, setup


requirements = [
    'click',
    'fabric',
    'paramiko>=3.4,<4',  # 5.0.0 breaks agent-key auth with fabric 3.2.3 (AgentKey.public_blob AttributeError)
    'nginxfmt',
    'python-dotenv',
    'ruff',
    'marko',
    'requests',
]


setup(
    python_requires='>=3.10',
    install_requires=requirements,
    packages=find_packages(),
)
