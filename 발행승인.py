"""불변 발행 후보와 명시적 승인의 경계. 외부 생성·수집은 수행하지 않는다."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


class PublicationError(ValueError):
    """입력값·비밀 URL을 포함하지 않는 고정 사유 코드."""


def _resolved(path):
    resolved = str(Path(path).resolve())
    # Windows에서 아직 생성 중인 하위 경로는 resolve가 확장 접두사를 남길 수 있다.
    # 같은 파일시스템 경로의 표기만 통일한 뒤 경계를 비교한다.
    if os.name == "nt":
        if resolved.startswith("\\\\?\\UNC\\"):
            resolved = "\\\\" + resolved[8:]
        elif resolved.startswith("\\\\?\\"):
            resolved = resolved[4:]
    return Path(resolved)


def _path(store, run_id, suffix=".json"):
    if (not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", run_id)
            or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", run_id, re.I)):
        raise PublicationError("invalid_run_id")
    root = _resolved(store)
    path = root / "cache" / "publications" / (run_id + suffix)
    resolved_folder = _resolved(path.parent)
    is_link = path.is_symlink()
    if not resolved_folder.is_relative_to(root) or is_link:
        raise PublicationError("unsafe_path")
    return path


def _validate_payload(payload):
    def text(value, maximum):
        return isinstance(value, str) and 0 < len(value) <= maximum

    bad = not isinstance(payload, dict) or set(payload) != {"username", "embeds", "allowed_mentions"}
    if bad or not text(payload["username"], 80) or payload["allowed_mentions"] != {"parse": []}:
        raise PublicationError("invalid_payload")
    embeds = payload["embeds"]
    if not isinstance(embeds, list) or not 1 <= len(embeds) <= 10:
        raise PublicationError("invalid_payload")
    count = 0
    for embed in embeds:
        if not isinstance(embed, dict) or not embed or set(embed) - {"title", "description", "url", "color", "footer", "author", "fields", "image", "thumbnail", "timestamp"}:
            raise PublicationError("invalid_payload")
        for key, maximum in (("title", 256), ("description", 4096), ("url", 2048), ("timestamp", 64)):
            if key in embed and not text(embed[key], maximum):
                raise PublicationError("invalid_payload")
        if "color" in embed and (type(embed["color"]) is not int or not 0 <= embed["color"] <= 0xFFFFFF):
            raise PublicationError("invalid_payload")
        for key, required, allowed in (("footer", "text", {"text", "icon_url"}), ("author", "name", {"name", "url", "icon_url"}),
                                       ("image", "url", {"url"}), ("thumbnail", "url", {"url"})):
            if key not in embed:
                continue
            obj = embed[key]
            if not isinstance(obj, dict) or required not in obj or set(obj) - allowed:
                raise PublicationError("invalid_payload")
            for name, value in obj.items():
                if not text(value, 256 if name == "name" else 2048):
                    raise PublicationError("invalid_payload")
        fields = embed.get("fields", [])
        if not isinstance(fields, list) or len(fields) > 25:
            raise PublicationError("invalid_payload")
        for field in fields:
            if (not isinstance(field, dict) or set(field) - {"name", "value", "inline"}
                    or not text(field.get("name"), 256) or not text(field.get("value"), 1024)
                    or ("inline" in field and type(field["inline"]) is not bool)):
                raise PublicationError("invalid_payload")
            count += len(field["name"]) + len(field["value"])
        count += len(embed.get("title", "")) + len(embed.get("description", ""))
        count += len(embed.get("footer", {}).get("text", "")) + len(embed.get("author", {}).get("name", ""))
    if count > 6000:
        raise PublicationError("invalid_payload")
    _canonical(payload)


def _create_immutable(path, value):
    """완전히 쓴 파일을 hard link로 원자적 생성한다. 기존 대상은 교체하지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".publication-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def _canonical(value):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise PublicationError("invalid_json") from None
    if re.search(rb"https?://(?:[a-z]+\.)?discord(?:app)?\.com/api(?:/v[0-9]+)?/webhooks/", raw, re.I):
        raise PublicationError("secret_not_allowed")
    return raw


def prepare_publication(store, run_id, embeds, *, username="편집실", metadata=None):
    path = _path(store, run_id)
    payload = copy.deepcopy({"username": username, "embeds": embeds, "allowed_mentions": {"parse": []}})
    _validate_payload(payload)
    manifest = {"run_id": run_id, "sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
                "payload": payload, "metadata": copy.deepcopy(metadata)}
    try:
        _create_immutable(path, manifest)
    except FileExistsError:
        existing = load_publication(store, run_id)
        if existing["payload"] != payload:
            raise PublicationError("candidate_exists") from None
        return existing
    except OSError:
        raise PublicationError("candidate_write_failed") from None
    return manifest


def load_publication(store, run_id):
    path = _path(store, run_id)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise PublicationError("candidate_unreadable") from None
    if (not isinstance(manifest, dict) or set(manifest) != {"run_id", "sha256", "payload", "metadata"}
            or manifest.get("run_id") != run_id or not isinstance(manifest.get("sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", manifest["sha256"])):
        raise PublicationError("invalid_manifest")
    _validate_payload(manifest["payload"])
    if hashlib.sha256(_canonical(manifest["payload"])).hexdigest() != manifest["sha256"]:
        raise PublicationError("hash_mismatch")
    _canonical(manifest)
    return manifest


def _supplied_fields_match(expected, actual):
    """Discord가 추가한 proxy/type 등은 무시하고 공급한 필드만 재귀 비교한다."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _supplied_fields_match(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(_supplied_fields_match(a, b) for a, b in zip(expected, actual))
    return type(expected) is type(actual) and expected == actual


def publish_prepared(store, run_id, confirmed_sha256, publisher):
    manifest = load_publication(store, run_id)
    if not isinstance(confirmed_sha256, str) or confirmed_sha256 != manifest["sha256"]:
        raise PublicationError("confirmation_mismatch")
    if (getattr(publisher, "live", False) is not True or not getattr(publisher, "webhook_url", None)
            or not callable(getattr(publisher, "publish", None)) or not callable(getattr(publisher, "read_message", None))):
        raise PublicationError("publisher_not_ready")
    claim_path = _path(store, run_id, ".claim.json")
    try:
        _create_immutable(claim_path, {"run_id": run_id, "sha256": manifest["sha256"], "state": "claimed"})
    except FileExistsError:
        raise PublicationError("already_claimed") from None
    except OSError:
        raise PublicationError("claim_failed") from None
    payload = manifest["payload"]
    try:
        result = publisher.publish(copy.deepcopy(payload["embeds"]), username=payload["username"])
    except Exception:
        # 요청이 도달했는지 알 수 없다. 예외 원문 대신 고정 코드, claim은 영구 유지한다.
        result = {"status": "failed", "error": "send_failed"}
    result["send_outcome"] = ("sent" if result.get("status") == "sent" else
                              "rejected" if result.get("http_status") and result["http_status"] < 500 else "unknown")
    if result.get("status") == "sent" and result.get("message_id"):
        try:
            read = publisher.read_message(result["message_id"])
        except Exception:
            read = {"status": "failed"}
        if read.get("status") == "read":
            message = read["message"]
            matches = _supplied_fields_match(payload["embeds"], message.get("embeds"))
            username = message.get("author", {}).get("username", message.get("username"))
            matches = matches and (username is None or username == payload["username"])
            result["readback"] = {"status": "verified" if matches else "mismatch"}
        else:
            result["readback"] = {"status": "failed"}
    else:
        result["readback"] = {"status": "unavailable"}
    _create_immutable(_path(store, run_id, ".receipt.json"), {"sha256": manifest["sha256"], "result": result})
    return result
