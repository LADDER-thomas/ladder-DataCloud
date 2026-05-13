"""
AWS 接続用セッション。

アクセスキーは本パッケージ直下の aws_credentials.json（JSON）に直接記載する方式を推奨する。
環境変数 DATACLOUD_AWS_CREDENTIALS_FILE で別パスを指定可能。ファイルが無い・キーが空のときは
環境変数 AWS_ACCESS_KEY_ID 等および ~/.aws/credentials（既定チェーン）にフォールバックする。

任意の厳格チェック: 環境変数 ``DATACLOUD_IAM_USER_ARN`` に **IAM ユーザーの完全な Arn**
（例: ``arn:aws:iam::123456789012:user/my-dev-user``）を設定したときだけ、
STS ``GetCallerIdentity`` の Arn と**完全一致**するか検証する。未設定のときは検証しない
（ECS タスクロール・AssumeRole・共有のローカルキーいずれもそのまま使える）。

環境変数 AWS_ASSUME_ROLE_ARN が設定されている場合のみ、既存の認証情報（環境変数・共有
クレデンシャルファイル等のデフォルトチェーン）で STS AssumeRole を実行し、その一時
認証情報で boto3 セッションを構築する（AssumeRole 経路では GetCallerIdentity による
上記ユーザ照合は行わない）。

OIDC / IRSA 等で AWS_WEB_IDENTITY_TOKEN_FILE が設定されている場合は二重に AssumeRole
しない（boto3 の既定チェーンに任せる）。

任意:
  AWS_ROLE_SESSION_NAME  AssumeRole のセッション名（既定: datacloud-export）
  AWS_ROLE_EXTERNAL_ID   クロスアカウント時の ExternalId
  DATACLOUD_IAM_USER_ARN  上記の opt-in ユーザ Arn 照合（未設定で照合なし）

疎通確認: verify_aws_credential_access()、または export_datacloud.py --verify-credentials。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from boto3.session import Session

_DATACLOUD_DIR = Path(__file__).resolve().parent
_DEFAULT_CREDENTIALS_PATH = _DATACLOUD_DIR / "aws_credentials.json"


def _credentials_config_paths() -> list[Path]:
    """読みに行く JSON パス（先勝ち）。"""
    out: list[Path] = []
    env = (os.environ.get("DATACLOUD_AWS_CREDENTIALS_FILE") or "").strip()
    if env:
        out.append(Path(env).expanduser())
    out.append(_DEFAULT_CREDENTIALS_PATH)
    return out


def load_aws_credentials_from_config() -> dict[str, str] | None:
    """
    aws_credentials.json からアクセスキーを読む。
    aws_access_key_id / aws_secret_access_key が両方とも非空なら dict を返す。
    aws_session_token は任意（一時クレデンシャル時）。
    """
    for path in _credentials_config_paths():
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            data: dict[str, Any] = json.loads(raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        ak = str(
            data.get("aws_access_key_id") or data.get("access_key_id") or ""
        ).strip()
        sk = str(
            data.get("aws_secret_access_key") or data.get("secret_access_key") or ""
        ).strip()
        if not ak or not sk:
            continue
        st = str(
            data.get("aws_session_token") or data.get("session_token") or ""
        ).strip()
        out: dict[str, str] = {
            "aws_access_key_id": ak,
            "aws_secret_access_key": sk,
        }
        if st:
            out["aws_session_token"] = st
        return out
    return None


def _base_boto3_session(region: str | None) -> "Session":
    """config ファイル優先、無ければ boto3 既定チェーン。"""
    import boto3

    c = load_aws_credentials_from_config()
    if c:
        kwargs: dict[str, str | None] = {
            "aws_access_key_id": c["aws_access_key_id"],
            "aws_secret_access_key": c["aws_secret_access_key"],
            "region_name": region,
        }
        if c.get("aws_session_token"):
            kwargs["aws_session_token"] = c["aws_session_token"]
        return boto3.Session(**kwargs)
    return boto3.Session(region_name=region)


class AwsCredentialVerifyResult(TypedDict, total=False):
    """verify_aws_credential_access の戻り値。"""

    Arn: str
    Account: str
    UserId: str
    credentials_source: str
    s3_head_bucket: str
    s3_ok: bool
    s3_error: str


def verify_aws_credential_access(
    region_name: str | None = None,
    *,
    test_s3_bucket: str | None = None,
    s3_soft_check: bool = False,
) -> AwsCredentialVerifyResult:
    """
    認証情報の疎通検証: get_boto3_session のあと STS GetCallerIdentity を実行する。
    （``DATACLOUD_IAM_USER_ARN`` 設定時のみ、同関数内でユーザ Arn 照合）。test_s3_bucket 指定時は S3 head_bucket も試行する。
    s3_soft_check True のとき S3 失敗は例外にせず s3_ok / s3_error に記録する。
    """
    from botocore.exceptions import BotoCoreError, ClientError

    region = _region_for_session(region_name)
    session = get_boto3_session(region_name)
    sts = session.client("sts", region_name=region)
    try:
        ident = sts.get_caller_identity()
    except (ClientError, BotoCoreError) as e:
        raise RuntimeError(f"STS GetCallerIdentity に失敗しました: {e}") from e

    src = (
        "aws_credentials.json"
        if load_aws_credentials_from_config()
        else "environment_or_shared_credentials"
    )
    out: AwsCredentialVerifyResult = {
        "Arn": str(ident.get("Arn", "") or ""),
        "Account": str(ident.get("Account", "") or ""),
        "UserId": str(ident.get("UserId", "") or ""),
        "credentials_source": src,
    }

    if not test_s3_bucket:
        return out

    s3 = session.client("s3", region_name=region)
    try:
        s3.head_bucket(Bucket=test_s3_bucket)
        out["s3_head_bucket"] = test_s3_bucket
        out["s3_ok"] = True
    except (ClientError, BotoCoreError) as e:
        out["s3_head_bucket"] = test_s3_bucket
        out["s3_ok"] = False
        out["s3_error"] = str(e)
        if not s3_soft_check:
            raise RuntimeError(
                f"S3 HeadBucket({test_s3_bucket!r}) に失敗しました: {e}"
            ) from e
    return out


def _region_for_session(region_name: str | None) -> str | None:
    r = (region_name or os.environ.get("AWS_DEFAULT_REGION") or "").strip()
    return r or None


def _explicit_role_arn() -> str:
    """AssumeRole 対象ロール ARN（未設定なら空）。"""
    v = (os.environ.get("AWS_ASSUME_ROLE_ARN") or "").strip()
    if v:
        return v
    # OIDC 等では AWS_ROLE_ARN が付くが、SDK が既にロールを引き受けているので二重にしない
    if (os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE") or "").strip():
        return ""
    return (os.environ.get("AWS_ROLE_ARN") or "").strip()


def sts_verify_iam_user_arn() -> str | None:
    """
    GetCallerIdentity と照合する IAM ユーザー ARN（**完全一致、opt-in**）。

    - 未設定: 照合しない（``None``）
    - ``skip`` / ``none`` / ``off`` / 空: 照合しない
    - 上記以外: その文字列を想定 Arn として照合（通常は ``arn:aws:iam::...:user/...``）
    """
    raw = os.environ.get("DATACLOUD_IAM_USER_ARN")
    if raw is None:
        return None
    v = raw.strip()
    if not v or v.lower() in ("skip", "none", "off"):
        return None
    return v


def _assert_caller_is_iam_user(
    session: "Session", region: str | None, expected_arn: str
) -> None:
    from botocore.exceptions import BotoCoreError, ClientError

    sts = session.client("sts", region_name=region)
    try:
        ident = sts.get_caller_identity()
    except (ClientError, BotoCoreError):
        raise
    got = (ident.get("Arn") or "").strip()
    if got != expected_arn:
        raise RuntimeError(
            "認証後の IAM 主体が DATACLOUD_IAM_USER_ARN と一致しません。"
            f" 想定: {expected_arn!r} 実際: {got!r}"
        )


def get_boto3_session(region_name: str | None = None) -> "Session":
    """boto3 Session。AWS_ASSUME_ROLE_ARN（または AWS_ROLE_ARN）があるときは STS で引き受け。"""
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    region = _region_for_session(region_name)
    role_arn = _explicit_role_arn()

    if not role_arn:
        session = _base_boto3_session(region)
        exp = sts_verify_iam_user_arn()
        if exp:
            _assert_caller_is_iam_user(session, region, exp)
        return session

    session_name = (os.environ.get("AWS_ROLE_SESSION_NAME") or "datacloud-export").strip()
    if not session_name:
        session_name = "datacloud-export"
    session_name = session_name[:64]

    sts = _base_boto3_session(region).client("sts")
    kwargs: dict[str, str] = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name,
    }
    ext = (os.environ.get("AWS_ROLE_EXTERNAL_ID") or "").strip()
    if ext:
        kwargs["ExternalId"] = ext

    try:
        resp = sts.assume_role(**kwargs)
    except (ClientError, BotoCoreError):
        raise

    creds = resp["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
