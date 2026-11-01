"""Pair this computer with a Google TV, once.   TV_HOST=<the TV's address> .venv/bin/python scripts/tv_pair.py

The TV shows a code; type it here. The key is kept in ~/.voicectl/tv (or TV_CERT_DIR), never in the repo."""
import asyncio
import os
import sys

from voicectl.tv.client import cert_dir


async def main() -> None:
    from androidtvremote2 import AndroidTVRemote, CannotConnect, InvalidAuth

    host = os.environ.get("TV_HOST") or input("The TV's address: ").strip()
    folder = cert_dir()
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    remote = AndroidTVRemote("voicectl", str(folder / "cert.pem"), str(folder / "key.pem"), host)
    await remote.async_generate_cert_if_missing()
    try:
        await remote.async_connect()
        print("Already paired.")
    except InvalidAuth:
        await remote.async_start_pairing()
        code = input("Code shown on the TV: ").strip()
        await remote.async_finish_pairing(code)
        await remote.async_connect()
        print("Paired.")
    except CannotConnect as e:
        sys.exit(f"Cannot reach the TV: {e}")
    print("TV:", remote.device_info)
    remote.disconnect()


asyncio.run(main())
