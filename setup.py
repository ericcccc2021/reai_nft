#!/usr/bin/env python

from setuptools import setup, find_packages

with open("README.md", "rt") as fh:
    long_description = fh.read()

dependencies = [
    "chia-dev-tools",
]

dev_dependencies = [
    "flake8",
    "mypy",
    "black",
]

setup(
    name="reai_nft",
    version="0.0.2",
    packages=find_packages(exclude=("tests",)),
    author="ericcccc2021",
    entry_points={
        "console_scripts": ["reai-nft = reai_nft.cmd:cli"],
    },
    package_data={
        "": ["*.clvm", "*.clvm.hex", "*.clib", "*.clsp", "*.clsp.hex"],
    },
    setup_requires=["setuptools_scm"],
    install_requires=dependencies,
    url="https://github.com/Chia-Network",
    license="https://opensource.org/licenses/MIT",
    description="",
    long_description=long_description,
    long_description_content_type="text/markdown",
    classifiers=[
        "Programming Language :: Python :: 3",
        "Topic :: Security :: Cryptography",
    ],
    extras_require=dict(
        dev=dev_dependencies,
    ),
    project_urls={
        "Source": "https://github.com/ericcccc2021/reai_nft.git",
    },
)
