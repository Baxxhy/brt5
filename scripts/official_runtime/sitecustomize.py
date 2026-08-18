"""Runtime-only Docker timeout adapter; does not modify SWTBench source."""

import docker


_original_from_env = docker.from_env


def _from_env_with_long_timeout(**kwargs):
    kwargs.setdefault("timeout", 600)
    return _original_from_env(**kwargs)


docker.from_env = _from_env_with_long_timeout
