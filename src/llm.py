"""Claude 클라이언트 생성 — API 키와 OAuth 토큰을 모두 받는다.

인증 방식이 두 가지이고 헤더가 다르다.

    ANTHROPIC_API_KEY        → x-api-key: <key>
    OAuth 토큰                → Authorization: Bearer <token>
                                + anthropic-beta: oauth-2025-04-20

Claude Code용으로 발급받은 `CLAUDE_CODE_OAUTH_TOKEN`은 후자다. 이름만 다를 뿐
같은 OAuth 토큰이므로, 이미 그게 등록돼 있으면 별도로 API 키를 발급받지 않아도
된다. 예전에는 `Anthropic()`만 호출해서 API 키가 없으면 무조건 실패했다.

주의: OAuth 토큰의 스코프가 Messages API 전반을 허용하는지는 호출해 봐야 안다.
막혀 있으면 401/403이 오고, 그때는 ANTHROPIC_API_KEY를 따로 발급받아야 한다.
"""

from __future__ import annotations

import os

from anthropic import Anthropic

OAUTH_BETA = "oauth-2025-04-20"
API_KEY_ENV = "ANTHROPIC_API_KEY"
OAUTH_ENVS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")

MISSING_HINT = (
    "Claude 인증 정보가 없습니다. 아래 중 하나를 등록하세요.\n"
    f"  {API_KEY_ENV}            — console.anthropic.com에서 발급한 API 키\n"
    f"  CLAUDE_CODE_OAUTH_TOKEN  — `claude setup-token`으로 발급한 OAuth 토큰\n"
    "  GitHub Actions: Settings → Secrets and variables → Actions")


def credential_kind() -> str | None:
    """어떤 자격 증명이 잡히는지. 값은 절대 반환하지 않는다."""
    if os.getenv(API_KEY_ENV):
        return API_KEY_ENV
    for name in OAUTH_ENVS:
        if os.getenv(name):
            return name
    return None


def has_credentials() -> bool:
    return credential_kind() is not None


def build_client(**kwargs) -> Anthropic:
    """있는 자격 증명으로 클라이언트를 만든다. API 키를 우선한다."""
    if os.getenv(API_KEY_ENV):
        return Anthropic(**kwargs)

    for name in OAUTH_ENVS:
        token = os.getenv(name)
        if token:
            # OAuth 토큰은 Bearer로 가야 하고 베타 헤더가 함께 필요하다.
            # x-api-key로 보내면 401이 나는데, 원인이 토큰 값 문제로 보여 헷갈린다.
            headers = {"anthropic-beta": OAUTH_BETA, **kwargs.pop("default_headers", {})}
            return Anthropic(auth_token=token, default_headers=headers, **kwargs)

    raise RuntimeError(MISSING_HINT)
