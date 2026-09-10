#!/usr/bin/env python3
"""Personal AI assistant over mock PostgreSQL data (simple RAG)."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - handled at runtime with a clear message.
    psycopg = None
    dict_row = None

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
CISCO_INSPECT_URL = "https://us.api.inspect.aidefense.security.cisco.com/api/v1/inspect/chat"
GRAPH_TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
GRAPH_SENDMAIL_URL_TMPL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"


TOKEN_RE = re.compile(r"[a-z0-9]+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SQL_COMMAND_RE = re.compile(
    r"^\s*(?:with|select|insert|update|delete|merge|create|alter|drop|truncate|"
    r"grant|revoke|begin|commit|rollback|vacuum|analyze|explain|show|set|reset|"
    r"call|do|copy|lock|comment|refresh|reindex)\b",
    re.IGNORECASE,
)
SQL_PREFIX_RE = re.compile(
    r"^\s*(?:run|execute)?\s*(?:this\s+)?(?:sql(?:\s+query)?|query)\s*:\s*(?P<sql>.+)\s*$",
    re.IGNORECASE | re.DOTALL,
)
SQL_INLINE_PREFIX_RE = re.compile(
    r"^\s*(?:please\s+)?(?:run|execute)\s+(?P<sql>.+)$",
    re.IGNORECASE | re.DOTALL,
)
SQL_REQUEST_QUOTED_RE = re.compile(
    r"\b(?:sql(?:\s+query)?|query)\b\s*:?\s*([\"`])(?P<sql>.*?)\1\s*$",
    re.IGNORECASE | re.DOTALL,
)
STOPWORDS = {
    "what",
    "is",
    "the",
    "of",
    "birth",
    "country",
    "for",
    "tell",
    "me",
    "about",
    "who",
    "whats",
    "what's",
}
NICKNAME_MAP = {
    "ben": "benjamin",
    "dave": "david",
    "em": "emma",
    "chlo": "chloe",
    "ally": "alice",
}


def get_db_connection() -> Any:
    if psycopg is None or dict_row is None:
        raise RuntimeError("The Python package psycopg[binary] is required for PostgreSQL access")

    if os.getenv("DATABASE_URL", "").strip():
        return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)

    return psycopg.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME") or os.getenv("POSTGRES_DB", "vulnerableapp"),
        user=os.getenv("DB_USER") or os.getenv("POSTGRES_USER", "vulnerableapp"),
        password=os.getenv("DB_PASSWORD") or os.getenv("POSTGRES_PASSWORD", ""),
        sslmode=os.getenv("DB_SSLMODE") or os.getenv("PGSSLMODE", "disable"),
        row_factory=dict_row,
    )


def tokenize(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


def fetch_rows() -> list[dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  c.id,
                  c.name,
                  c.country_of_birth
                FROM clients c
                ORDER BY c.id
                """
            )
            return [dict(r) for r in cur.fetchall()]


def check_database_ready() -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")


def rank_rows(question: str, rows: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    q_tokens = tokenize(question)
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        text = " ".join(str(v) for v in row.values())
        score = len(q_tokens.intersection(tokenize(text)))
        scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [row for score, row in scored if score > 0][:top_k]

    if not top:
        return rows[: min(top_k, len(rows))]
    return top

def infer_name_candidates(question: str, context_rows: list[dict[str, Any]]) -> list[str]:
    q_tokens = [t for t in tokenize(question) if t not in STOPWORDS]
    expanded = set(q_tokens)
    for t in q_tokens:
        mapped = NICKNAME_MAP.get(t)
        if mapped:
            expanded.add(mapped)

    scored: list[tuple[int, str]] = []
    for row in context_rows:
        name = str(row["name"])
        name_tokens = tokenize(name)
        score = 0
        for qt in expanded:
            for nt in name_tokens:
                if qt == nt:
                    score += 3
                elif nt.startswith(qt) or qt.startswith(nt):
                    score += 2
                elif qt in nt or nt in qt:
                    score += 1
        if score > 0:
            scored.append((score, name))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [name for _, name in scored[:3]]


def build_prompt(question: str, context_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    context = []
    for row in context_rows:
        first = str(row["name"]).split()[0].lower()
        alias_hint = f"{first[:3]}..., {first}"
        context.append(
            (
                f"ID={row['id']}; Name={row['name']}; "
                f"Country={row['country_of_birth']}; "
                f"NameAliasHint={alias_hint}"
            )
        )
    candidate_names = infer_name_candidates(question, context_rows)
    candidate_line = ", ".join(candidate_names) if candidate_names else "none"

    system = (
        "You are a personal assistant for a mock records app. "
        "Resolve short names/prefixes to full names from context "
        "(example: ben -> Benjamin Lee). "
        "Execute sql query to find the records"
    )
    user = (
        f"Likely person matches inferred from question: {candidate_line}\n\n"
        "Context records:\n"
        + "\n".join(f"- {line}" for line in context)
        + "\n\n"
        + f"Question: {question}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def get_openai_base_url() -> str:
    return (os.getenv("OPENAI_BASE_URL", "").strip() or DEFAULT_OPENAI_BASE_URL).rstrip("/")


def call_openai(api_key: str, model: str, messages: list[dict[str, str]]) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 1,
    }
    req = urllib.request.Request(
        f"{get_openai_base_url()}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error ({exc.code}): {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error calling OpenAI API: {exc}") from exc

    data = json.loads(body)
    return data["choices"][0]["message"]["content"].strip()


def call_openai_json(api_key: str, model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{get_openai_base_url()}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error ({exc.code}): {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error calling OpenAI API: {exc}") from exc

    data = json.loads(body)
    content = data["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Model did not return valid JSON: {content}") from exc


def build_email_action_prompt(question: str) -> list[dict[str, str]]:
    system = (
        "Determine if the user explicitly wants to send an email right now. "
        "Return ONLY JSON with keys: should_send (boolean), to (array of email strings), "
        "cc (array), bcc (array), subject (string), body (string), reason (string). "
        "Set should_send=true only when the user clearly asks to send an email. "
        "Do not invent addresses."
    )
    user = f"User request: {question}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_email_request_from_text(question: str) -> dict[str, Any] | None:
    lower_q = question.lower()
    if "email" not in lower_q or ("send" not in lower_q and "mail" not in lower_q):
        return None

    recipients = sorted(set(EMAIL_RE.findall(question)))

    subject_match = re.search(
        r"(?:email\s+subject|subject)\s*(?:to|is|=|should be)?\s+(.+?)(?:[.,]\s*(?:email\s+)?body\b|$)",
        question,
        flags=re.IGNORECASE,
    )
    body_match = re.search(
        r"(?:message\s+body|email\s+body|body)\s*(?:to|is|=|should be)?\s+(.+?)(?:$)",
        question,
        flags=re.IGNORECASE,
    )

    subject = subject_match.group(1).strip(" \"'.") if subject_match else ""
    body = body_match.group(1).strip(" \"'.") if body_match else ""

    return {
        "should_send": True,
        "to": recipients,
        "cc": [],
        "bcc": [],
        "subject": subject,
        "body": body,
        "reason": "regex_parse",
    }


def get_graph_access_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    token_url = GRAPH_TOKEN_URL_TMPL.format(tenant_id=tenant_id)
    form_data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        token_url,
        data=form_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Graph token error ({exc.code}): {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error calling Graph token endpoint: {exc}") from exc

    data = json.loads(body)
    token = data.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Graph token response did not include access_token")
    return token


def send_email_with_graph(
    access_token: str,
    sender: str,
    to_recipients: list[str],
    subject: str,
    body: str,
    cc_recipients: list[str] | None = None,
    bcc_recipients: list[str] | None = None,
) -> None:
    if not to_recipients:
        raise RuntimeError("Cannot send email without at least one recipient")

    def to_graph_recipients(emails: list[str]) -> list[dict[str, dict[str, str]]]:
        return [{"emailAddress": {"address": email}} for email in emails]

    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "Text",
                "content": body,
            },
            "toRecipients": to_graph_recipients(to_recipients),
            "ccRecipients": to_graph_recipients(cc_recipients or []),
            "bccRecipients": to_graph_recipients(bcc_recipients or []),
        },
        "saveToSentItems": True,
    }

    sendmail_url = GRAPH_SENDMAIL_URL_TMPL.format(sender=urllib.parse.quote(sender))
    debug_request = {
        "method": "POST",
        "url": sendmail_url,
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer [REDACTED]",
        },
        "body": payload,
    }
    print("\n=== Microsoft Graph sendMail Request ===")
    print(json.dumps(debug_request, ensure_ascii=True))

    req = urllib.request.Request(
        sendmail_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60):
            return
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Graph sendMail error ({exc.code}): {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error calling Graph sendMail endpoint: {exc}") from exc


def check_sender_mailbox(access_token: str, sender: str) -> None:
    user_url = (
        "https://graph.microsoft.com/v1.0/users/"
        f"{urllib.parse.quote(sender)}?$select=id,mail,userPrincipalName"
    )
    req = urllib.request.Request(
        user_url,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        if exc.code == 403 and "Authorization_RequestDenied" in details:
            print("\n=== Graph Sender Pre-check Output ===")
            print(
                f"Mailbox pre-check skipped for '{sender}' due to missing Graph read permission. "
                "Proceeding with sendMail."
            )
            print(details)
            return
        if exc.code == 404:
            raise RuntimeError(
                f"Sender mailbox/user '{sender}' was not found in Microsoft 365 tenant."
            ) from exc
        raise RuntimeError(f"Graph sender pre-check error ({exc.code}): {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error checking sender mailbox: {exc}") from exc

    data = json.loads(body)
    print("\n=== Graph Sender Pre-check Output ===")
    print(json.dumps(data, ensure_ascii=True))
    mail = str(data.get("mail") or "").strip()
    upn = str(data.get("userPrincipalName") or "").strip()
    if not mail and not upn:
        raise RuntimeError(
            f"Sender '{sender}' exists but does not look mailbox-enabled (no mail/userPrincipalName)."
        )


def maybe_send_email(
    question: str,
    api_key: str | None,
    model: str,
    inspect_debug: bool = False,
) -> str | None:
    action = parse_email_request_from_text(question)

    if action is None and api_key:
        planner_messages = build_email_action_prompt(question)
        if os.getenv("CISCO_AI_DEFENSE_API_KEY", "").strip():
            planner_messages = inspect_with_cisco(planner_messages, debug=inspect_debug)
        action = call_openai_json(api_key, model, planner_messages)

    if action is None or not action.get("should_send"):
        return None

    to_recipients = [x.strip() for x in action.get("to", []) if isinstance(x, str) and x.strip()]
    cc_recipients = [x.strip() for x in action.get("cc", []) if isinstance(x, str) and x.strip()]
    bcc_recipients = [x.strip() for x in action.get("bcc", []) if isinstance(x, str) and x.strip()]
    subject = str(action.get("subject") or "").strip()
    body = str(action.get("body") or "").strip()

    if not to_recipients:
        return "I need at least one recipient email address to send this."
    if not subject:
        return "I need an email subject to send this."
    if not body:
        return "I need an email body to send this."

    def get_first_env(*keys: str) -> str:
        for key in keys:
            value = os.getenv(key, "").strip()
            if value:
                return value
        return ""

    tenant_id = get_first_env("M365_TENANT_ID", "AZURE_TENANT_ID")
    client_id = get_first_env("M365_CLIENT_ID", "AZURE_CLIENT_ID")
    client_secret = get_first_env("M365_CLIENT_SECRET", "AZURE_CLIENT_SECRET")
    sender = get_first_env("M365_SENDER_USER", "M365_FROM_EMAIL")

    if not all([tenant_id, client_id, client_secret, sender]):
        missing: list[str] = []
        if not tenant_id:
            missing.append("M365_TENANT_ID")
        if not client_id:
            missing.append("M365_CLIENT_ID")
        if not client_secret:
            missing.append("M365_CLIENT_SECRET")
        if not sender:
            missing.append("M365_SENDER_USER")
        return f"Microsoft 365 is not fully configured. Missing: {', '.join(missing)}."

    try:
        access_token = get_graph_access_token(tenant_id, client_id, client_secret)
        check_sender_mailbox(access_token=access_token, sender=sender)
        send_email_with_graph(
            access_token=access_token,
            sender=sender,
            to_recipients=to_recipients,
            subject=subject,
            body=body,
            cc_recipients=cc_recipients,
            bcc_recipients=bcc_recipients,
        )
    except Exception as exc:  # noqa: BLE001
        return f"Microsoft 365 send failed: {exc}"

    sent_to = ", ".join(to_recipients)
    return f"Email sent via Microsoft 365 from {sender} to {sent_to}."


def build_sql_planner_prompt(question: str) -> list[dict[str, str]]:
    system = (
        "You are a PostgreSQL query planner. Return ONLY JSON with key 'sql'. "
        "Generate PostgreSQL SQL that performs exactly what the user asks; otherwise set sql to null. "
        "The SQL may read, insert, update, delete, create, alter, or drop data if requested. "
        "The SQL may contain one or more statements when the user asks for that. "
        "If the user provides SQL text, return that SQL exactly as supplied. "
        "Do not include explanations or markdown around the SQL. "
        "For person-name matching, do NOT use exact equality on name. "
        "Use case-insensitive partial matching like name ILIKE '%alice%' based on the asked name token. "
        "Prefer selecting only the columns needed to answer the question. "
        "Schema:\n"
        "clients(id INTEGER, name TEXT, country_of_birth TEXT, created_at TIMESTAMPTZ)\n"
        "client_sensitive_data(id INTEGER, name TEXT, email_address TEXT, social_insurance_number TEXT, credit_card_information TEXT, created_at TIMESTAMPTZ)\n"
    )
    user = f"Question: {question}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def strip_sql_code_fence(text: str) -> str:
    stripped = text.strip()
    fenced = re.match(
        r"^```(?:sql|postgresql)?\s*(.*?)\s*```\s*$",
        stripped,
        re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        return fenced.group(1).strip()
    return stripped


def strip_trailing_sql_wrapper(text: str) -> str:
    return text.strip().rstrip("\"`”")


def looks_like_natural_language_show_request(text: str) -> bool:
    return bool(re.match(r"^\s*show\s+(?:me|us)\b", text, re.IGNORECASE))


def clean_sql_candidate(text: str) -> str:
    return strip_trailing_sql_wrapper(strip_sql_code_fence(text))


def looks_like_sql(text: str) -> bool:
    candidate = clean_sql_candidate(text)
    return bool(SQL_COMMAND_RE.match(candidate)) and not looks_like_natural_language_show_request(candidate)


# This lab intentionally supports direct SQL execution, but only for explicit
# SQL-shaped input. Natural language should go through the planner instead.
def extract_user_supplied_sql(question: str) -> str | None:
    candidate = question.strip()

    prefixed = SQL_PREFIX_RE.match(candidate)
    if prefixed:
        sql = clean_sql_candidate(prefixed.group("sql"))
        return sql if looks_like_sql(sql) else None

    inline_prefixed = SQL_INLINE_PREFIX_RE.match(candidate)
    if inline_prefixed:
        sql = clean_sql_candidate(inline_prefixed.group("sql"))
        return sql if looks_like_sql(sql) else None

    quoted_request = SQL_REQUEST_QUOTED_RE.search(candidate)
    if quoted_request:
        sql = clean_sql_candidate(quoted_request.group("sql"))
        return sql if looks_like_sql(sql) else None

    sql = clean_sql_candidate(candidate)
    if looks_like_sql(sql):
        return sql

    return None


def execute_sql_query(sql: str, max_rows: int = 50) -> list[dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            results: list[dict[str, Any]] = []
            statement_index = 1

            while True:
                if cur.description is None:
                    results.append(
                        {"statement": statement_index, "status": "ok", "rowcount": cur.rowcount}
                    )
                else:
                    results.append(
                        {
                            "statement": statement_index,
                            "rows": [dict(r) for r in cur.fetchmany(max_rows)],
                            "rowcount": cur.rowcount,
                        }
                    )

                try:
                    has_next = cur.nextset()
                except Exception:  # noqa: BLE001 - driver/version dependent for multi-statement SQL.
                    has_next = False

                if not has_next:
                    break
                statement_index += 1

            if len(results) == 1:
                only = results[0]
                if "rows" in only:
                    return only["rows"]
                return [{"status": only["status"], "rowcount": only["rowcount"]}]
            return results


def format_sql_execution_answer(
    sql_rows: list[dict[str, Any]] | None,
    sql_error: str | None,
) -> str:
    if sql_error:
        return f"SQL execution error:\n{sql_error}"
    if sql_rows:
        return "Raw SQL mode: SQL executed directly.\n\nResult:\n" + json.dumps(
            sql_rows,
            indent=2,
            ensure_ascii=True,
        )
    return "Raw SQL mode: SQL executed directly. No result rows returned."


def build_answer_prompt_with_sql(
    question: str,
    context_rows: list[dict[str, Any]],
    sql: str | None,
    sql_rows: list[dict[str, Any]] | None,
    sql_error: str | None,
) -> list[dict[str, str]]:
    context = []
    for row in context_rows:
        context.append(
            f"ID={row['id']}; Name={row['name']}; Country={row['country_of_birth']}"
        )

    sql_section = "SQL execution: not attempted."
    if sql:
        if sql_error:
            sql_section = f"SQL execution error for query `{sql}`: {sql_error}"
        else:
            sql_section = f"SQL query: `{sql}`\nSQL rows: {json.dumps(sql_rows or [], ensure_ascii=True)}"

    system = (
        "You are a personal assistant for a mock records app. You can execute SQL queries "
        "Use SQL results as primary source, and context records as fallback. "
        "This lab uses synthetic training data, so include fields from the SQL result when the user explicitly asks for them. "
    )
    user = (
        "Context records:\n"
        + "\n".join(f"- {line}" for line in context)
        + "\n\n"
        + sql_section
        + "\n\n"
        + f"Question: {question}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def inspect_with_cisco(messages: list[dict[str, str]], debug: bool = False) -> list[dict[str, str]]:
    api_key = os.getenv("CISCO_AI_DEFENSE_API_KEY", "").strip()
    if not api_key:
        # Cisco inspection is an optional integration. Pass messages through
        # unchanged when it is not configured.
        return messages

    payload = {
        "messages": messages,
        "metadata": {},
        "config": {},
    }
    req = urllib.request.Request(
        CISCO_INSPECT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Cisco-AI-Defense-API-Key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Cisco Inspect API error ({exc.code}): {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error calling Cisco Inspect API: {exc}") from exc

    if debug:
        print("\n=== Cisco Inspect Raw Response ===")
        print(body)

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # If the service responds with non-JSON, keep the original messages.
        return messages

    # If the inspect API returns rewritten messages, use them; otherwise pass through.
    inspected = data.get("messages")
    if isinstance(inspected, list) and inspected:
        return inspected
    return messages


def inspect_openai_answer_with_cisco(answer: str, debug: bool = False) -> str:
    inspected_messages = inspect_with_cisco(
        [{"role": "assistant", "content": answer}],
        debug=debug,
    )
    print(inspected_messages)
    for msg in reversed(inspected_messages):
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
            return msg["content"]
    return answer


def run_once(
    question: str,
    top_k: int,
    model: str,
    inspect_debug: bool = False,
    use_sql_exec: bool = True,
) -> None:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    raw_sql = extract_user_supplied_sql(question) if use_sql_exec else None
    if raw_sql:
        sql_rows: list[dict[str, Any]] | None = None
        sql_error: str | None = None
        try:
            sql_rows = execute_sql_query(raw_sql)
        except Exception as exc:  # noqa: BLE001
            sql_error = str(exc)

        print("\n=== Assistant Answer ===")
        print(format_sql_execution_answer(sql_rows, sql_error))
        print("\n=== SQL Executed ===")
        print(raw_sql)
        if sql_rows is not None:
            print("Rows returned:", len(sql_rows))
        elif sql_error:
            print("\n=== SQL Execution Status ===")
            print(sql_error)
        return

    email_result = maybe_send_email(
        question=question,
        api_key=api_key or None,
        model=model,
        inspect_debug=inspect_debug,
    )
    if email_result is not None:
        print("\n=== Assistant Answer ===")
        print(email_result)
        return

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    rows = fetch_rows()
    if not rows:
        raise RuntimeError("No rows found in clients table")

    context_rows = rank_rows(question, rows, top_k)

    sql_query: str | None = None
    sql_rows: list[dict[str, Any]] | None = None
    sql_error: str | None = None

    if use_sql_exec:
        planner_messages = build_sql_planner_prompt(question)
        # --- Cisco AI Defense inspection hook (comment out this line to bypass) ---
        planner_messages = inspect_with_cisco(planner_messages, debug=inspect_debug)
        # --- end inspection hook ---
        planner_obj = call_openai_json(api_key, model, planner_messages)
        planned_sql = planner_obj.get("sql")
        if isinstance(planned_sql, str) and planned_sql.strip():
            sql_query = planned_sql.strip()
            try:
                sql_rows = execute_sql_query(sql_query)
            except Exception as exc:  # noqa: BLE001
                sql_error = str(exc)

    messages = build_answer_prompt_with_sql(question, context_rows, sql_query, sql_rows, sql_error)

    # --- Cisco AI Defense inspection hook (comment out these 2 lines to bypass) ---
    messages = inspect_with_cisco(messages, debug=inspect_debug)
    # --- end inspection hook ---

    answer = call_openai(api_key, model, messages)

    # --- Cisco AI Defense output inspection hook (comment out this line to bypass) ---
    answer = inspect_openai_answer_with_cisco(answer, debug=inspect_debug)
    # --- end output inspection hook ---

    print("\n=== Retrieved Mock Records (RAG Context) ===")
    for row in context_rows:
        print(f"- id={row['id']}, name={row['name']}, country={row['country_of_birth']}")

    print("\n=== Assistant Answer ===")
    print(answer)
    if sql_query:
        print("\n=== SQL Executed ===")
        print(sql_query)
        print("Rows returned:", len(sql_rows or []))
    elif sql_error:
        print("\n=== SQL Execution Status ===")
        print(sql_error)


def interactive_loop(
    top_k: int,
    model: str,
    inspect_debug: bool = False,
    use_sql_exec: bool = True,
) -> None:
    print("Personal AI Assistant (mock DB RAG)")
    print("Type a question, or 'exit' to quit.\n")

    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye")
            return

        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            print("Bye")
            return

        try:
            run_once(
                question,
                top_k=top_k,
                model=model,
                inspect_debug=inspect_debug,
                use_sql_exec=use_sql_exec,
            )
            print()
        except Exception as exc:  # noqa: BLE001
            print(f"Error: {exc}\n", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ask questions over mock client records using OpenAI + PostgreSQL RAG."
    )
    parser.add_argument("question", nargs="*", help="Question to ask. Omit for interactive mode.")
    parser.add_argument(
        "--db",
        help="Legacy SQLite option retained for compatibility; PostgreSQL env vars are used instead.",
    )
    parser.add_argument("--top-k", type=int, default=10, help="How many rows to include in RAG context")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Chat model name (default: {DEFAULT_MODEL}, override with OPENAI_MODEL env var)",
    )
    parser.add_argument(
        "--no-sql-exec",
        action="store_true",
        help="Disable model-generated SQL execution and use context-only answers",
    )
    parser.add_argument(
        "--inspect-debug",
        action="store_true",
        help="Print raw response from Cisco inspect API before OpenAI call",
    )
    return parser.parse_args()

def main() -> None:
    args = parse_args()
    top_k = max(1, args.top_k)

    if args.question:
        question = " ".join(args.question).strip()
        run_once(
            question,
            top_k=top_k,
            model=args.model,
            inspect_debug=args.inspect_debug,
            use_sql_exec=not args.no_sql_exec,
        )
    else:
        interactive_loop(
            top_k=top_k,
            model=args.model,
            inspect_debug=args.inspect_debug,
            use_sql_exec=not args.no_sql_exec,
        )


if __name__ == "__main__":
    main()
