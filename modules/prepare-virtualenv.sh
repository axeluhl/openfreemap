#!/usr/bin/env bash

find . -name "*.egg-info" -exec rm -rf {} +
find . -name __pycache__ -exec rm -rf {} +

# AL2023's default python3 is 3.9, which is too old (OFM needs >=3.10),
# so build the venv from python3.11 (installed by pkg_base).
rm -rf venv
python3.11 -m venv venv

venv/bin/pip -V

venv/bin/pip install -U pip wheel setuptools



