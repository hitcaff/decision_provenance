from setuptools import setup, find_packages

setup(
    name="decision-provenance",
    version="1.1.1",
    description="Tamper-evident audit logging for ML inference pipelines",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[],
    extras_require={
        "ipfs": ["requests>=2.28"],
        "evm":  ["web3>=6.0"],
        "api":  ["fastapi>=0.100", "uvicorn>=0.22", "pydantic>=2.0"],
        "all":  ["requests>=2.28", "web3>=6.0", "fastapi>=0.100",
                 "uvicorn>=0.22", "pydantic>=2.0"],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
