from pathlib import Path

from setuptools import find_packages, setup


ROOT = Path(__file__).parent


setup(
    name="gemini-live-websocket-proxy",
    version="0.1.0",
    description="A transparent asynchronous WebSocket proxy for the Gemini Live API",
    long_description=(ROOT / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.8",
    install_requires=[
        "fastapi==0.115.13",
        "pydantic==1.10.22",
        "python-dotenv==1.0.1",
        "starlette==0.41.3",
        "uvicorn[standard]==0.30.6",
        "websockets==13.1",
    ],
    extras_require={
        "dev": [
            "pytest==8.3.5",
            "pytest-asyncio==0.24.0",
        ]
    },
    entry_points={
        "console_scripts": [
            "gemini-live-proxy=gemini_live_proxy.cli:main",
        ]
    },
)
