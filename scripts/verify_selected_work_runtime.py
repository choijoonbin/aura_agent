"""Exercise selected-work Ask using an owned, non-sensitive local verification task."""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse
from uuid import uuid4

import httpx


def verify() -> dict:
    base = os.getenv("DWP_GATEWAY_URL", "http://localhost:8080").rstrip("/")
    if urlparse(base).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("This disposable-fixture check is restricted to the local runtime.")
    tenant = os.getenv("DWP_SMOKE_TENANT_ID", "1")
    report: dict = {"environment": "local", "automaticBusinessExecution": False}
    task = None
    conversations: set[str] = set()
    with httpx.Client(base_url=base, timeout=55, headers={"X-Tenant-ID": tenant}) as client:
        def command(method, path, body=None, **headers):
            csrf = client.get("/api/auth/csrf")
            csrf.raise_for_status()
            token = csrf.json()["data"]
            return client.request(method, path, json=body, headers={
                **headers, token["headerName"]: token["token"],
            })

        def ask(body):
            response = command("POST", "/api/agent/v1/ask/stream", body)
            result = {"httpStatus": response.status_code, "stages": []}
            answer = None
            if response.status_code == 200:
                for frame in response.text.split("\n\n"):
                    lines = frame.splitlines()
                    event = next((line[7:] for line in lines if line.startswith("event: ")), "")
                    data = next((line[6:] for line in lines if line.startswith("data: ")), None)
                    if data is None:
                        continue
                    payload = json.loads(data)
                    if event == "progress":
                        result["stages"].append(payload.get("stage"))
                    elif event == "error":
                        result["errorCode"] = payload.get("code")
                    elif event == "result":
                        answer = payload["data"]
                        if answer.get("conversationId"):
                            conversations.add(answer["conversationId"])
                        result.update({
                            "state": answer["state"], "statusCode": answer["statusCode"],
                            "provider": answer["modelRoute"].get("provider"),
                            "modelState": answer["modelRoute"]["state"],
                            "citationCount": len(answer["citations"]),
                            "conversationCreated": bool(answer.get("conversationId")),
                            "mutationAllowed": answer["policy"]["mutationAllowed"],
                        })
            return result, answer

        try:
            login = command("POST", "/api/auth/login", {
                "tenantId": tenant, "email": os.getenv("DWP_SMOKE_EMAIL", "joonbin@sk.com"),
                "password": os.getenv("DWP_SMOKE_PASSWORD", "admin1234!"),
            })
            report["loginStatus"] = login.status_code
            login.raise_for_status()
            me = client.get("/api/auth/me", params={"permissionPrefix": "APP.ASK,APP.WORK"})
            me.raise_for_status()
            principal = me.json()["data"]
            grants = {
                f"{item['resourceKey']}:{item['permissionCode']}"
                for item in principal.get("permissions", []) if item.get("effect") == "ALLOW"
            }
            report["askAndWorkAllowed"] = {"APP.ASK:VIEW", "APP.WORK:VIEW"}.issubset(grants)
            report["sessionBindingPresent"] = bool(principal.get("sessionFamilyId"))
            with httpx.Client(base_url=base, timeout=5, headers={
                "Cookie": f"DWP_SESSION={client.cookies.get('DWP_SESSION')}",
                "X-Tenant-ID": tenant,
            }) as owner_client:
                recheck = owner_client.get("/api/auth/me", params={"permissionPrefix": "APP.ASK,APP.WORK"})
                report["ownerSessionRecheckStatus"] = recheck.status_code
            if not report["askAndWorkAllowed"]:
                return {**report, "blocked": "APP_PERMISSION_REQUIRED"}
            created = command("POST", "/api/platform/v1/workspace/work-hub/personal-tasks", {
                "title": "DWAI selected-work integration verification",
                "description": "Non-sensitive test fixture. Review the current version; no business action is required.",
                "priority": "LOW",
            }, **{"Idempotency-Key": str(uuid4())})
            report["fixtureCreateStatus"] = created.status_code
            if created.status_code not in {200, 201}:
                return {**report, "blocked": "FIXTURE_CREATE_DENIED"}
            task = created.json()["data"]
            selection = {
                "sourceSystem": "PERSONAL_TASK", "sourceReference": task["taskId"],
                "expectedVersion": task["version"],
            }
            body = {
                "requestId": str(uuid4()), "query": "What is verified about this selected task, and which original evidence is missing?",
                "locale": "en", "agentKey": "DWP_ASSISTANT", "sourceScopes": ["WORK_ITEM"],
                "pageContext": {
                    "route": "/work/queue", "appKey": "APP.WORK", "surface": "selected-work-assist",
                    "entityType": "PERSONAL_TASK", "entityRef": task["taskId"], "selectedWork": selection,
                },
            }
            report["stream"], answer = ask(body)
            if answer and answer.get("conversationId"):
                path = f"/api/agent/v1/conversations/{answer['conversationId']}"
                conversation = client.get(path)
                report["conversationReadStatus"] = conversation.status_code
                if conversation.status_code == 200:
                    messages = conversation.json()["data"]["messages"]
                    report["conversationMessageCount"] = len(messages)
                    report["persistedAnswerMatches"] = bool(answer.get("answer")) and messages[-1]["content"] == answer["answer"]
                followup = {**body, "requestId": str(uuid4()), "conversationId": answer["conversationId"],
                    "query": "Which original evidence is still missing?",
                    "pageContext": {"route": "/dwaion/new", "appKey": "APP.ASK", "surface": "workspace"}}
                report["continuation"], continued = ask(followup)
                report["sameConversationContinued"] = bool(continued) and continued.get("conversationId") == answer["conversationId"]
            current = client.get(f"/api/platform/v1/workspace/work-hub/personal-tasks/{task['taskId']}")
            current.raise_for_status()
            report["sourceUnchanged"] = current.json()["data"] == task
            body["requestId"] = str(uuid4())
            body["pageContext"]["selectedWork"] = {**selection, "expectedVersion": task["version"] + 1}
            report["staleVersion"], _ = ask(body)
            return report
        except (httpx.HTTPError, KeyError, ValueError) as error:
            report["blocked"] = type(error).__name__
            return report
        finally:
            if task is not None:
                cleanup = command("POST", f"/api/platform/v1/workspace/work-hub/personal-tasks/{task['taskId']}/delete",
                    {"version": task["version"]}, **{"Idempotency-Key": str(uuid4())})
                report["fixtureDeleteStatus"] = cleanup.status_code
            report["conversationCleanupStatuses"] = [
                command("DELETE", f"/api/agent/v1/conversations/{identifier}").status_code
                for identifier in sorted(conversations)
            ]
            report["logoutStatus"] = command("POST", "/api/auth/logout").status_code


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
