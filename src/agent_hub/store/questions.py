from __future__ import annotations

import time
import uuid
from typing import Any

from .base import _json


class QuestionStore:
    """Actor-to-actor questions with lease/claim/answer semantics.

    A question is the lightweight counterpart of a task control: the asker
    blocks on it (`hub_ask`), the target worker claims it and answers, and
    the answer flows back through the shared event stream. Statuses:
    pending -> claimed -> answered | expired | unsupported.
    """

    QUESTION_TTL_SECONDS = 30 * 60

    def create_question(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        asker_actor_id: str,
        asker_task_id: str | None,
        asker_session_id: str | None,
        target_actor_id: str,
        target_session_id: str | None = None,
        message: str,
        require: str | None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            asker = self._actor(asker_actor_id)
            if asker["tenant_id"] != tenant_id:
                raise PermissionError("asker does not belong to tenant")
            if asker["principal_id"] != principal_id:
                raise ValueError("asker does not belong to principal")
            target = self._actor(target_actor_id)
            if target["tenant_id"] != tenant_id:
                raise PermissionError("target does not belong to tenant")
            if target["principal_id"] != principal_id:
                raise PermissionError("target must belong to the same principal")
            question_id = f"question_{uuid.uuid4().hex}"
            status = "pending"
            if self._actor_node_online(target_actor_id, tenant_id) is None:
                status = "unsupported"
            self._connection.execute(
                """
                INSERT INTO hub_questions(
                    question_id, tenant_id, principal_id, asker_actor_id,
                    asker_task_id, asker_session_id, target_actor_id,
                    target_session_id, message,
                    require, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question_id,
                    tenant_id,
                    principal_id,
                    asker_actor_id,
                    asker_task_id,
                    asker_session_id,
                    target_actor_id,
                    target_session_id,
                    message[:50_000],
                    require[:10_000] if require else None,
                    status,
                    now,
                ),
            )
            self._condition.notify_all()
            return self._question(question_id)

    def claim_questions(
        self,
        *,
        actor_id: str,
        node_id: str,
        limit: int = 5,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 20)
        now = time.time()
        with self._condition, self._connection:
            node = self._node(node_id)
            if node["actor_id"] != actor_id:
                raise PermissionError("node does not belong to actor")
            if tenant_id is not None and node["tenant_id"] != tenant_id:
                raise PermissionError("node does not belong to tenant")
            rows = self._connection.execute(
                """
                SELECT question_id FROM hub_questions
                WHERE target_actor_id=? AND status='pending' AND tenant_id=?
                ORDER BY seq LIMIT ?
                """,
                (actor_id, node["tenant_id"], limit),
            ).fetchall()
            questions: list[dict[str, Any]] = []
            for row in rows:
                question_id = str(row["question_id"])
                lease_token = uuid.uuid4().hex + uuid.uuid4().hex
                self._connection.execute(
                    """
                    UPDATE hub_questions
                    SET status='claimed', lease_token=?, lease_until=?, claimed_at=?
                    WHERE question_id=?
                    """,
                    (lease_token, now + 120, now, question_id),
                )
                question = self._question(question_id)
                question["lease_token"] = lease_token
                questions.append(question)
            self._condition.notify_all()
            return questions

    def answer_question(
        self,
        question_id: str,
        *,
        lease_token: str,
        answer_text: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            question = self._question(question_id)
            if tenant_id is not None and question["tenant_id"] != tenant_id:
                raise LookupError("question not found")
            if question["status"] == "answered":
                return question
            if question["status"] not in {"pending", "claimed"}:
                raise PermissionError(
                    f"question cannot be answered from {question['status']}"
                )
            row = self._connection.execute(
                "SELECT lease_token, lease_until FROM hub_questions WHERE question_id=?",
                (question_id,),
            ).fetchone()
            if row is None:
                raise LookupError("question not found")
            if str(row["lease_token"] or "") != lease_token:
                raise PermissionError("invalid question lease")
            if float(row["lease_until"] or 0) < now:
                raise PermissionError("question lease expired")
            self._connection.execute(
                """
                UPDATE hub_questions
                SET status='answered', answer_text=?, answered_at=?, lease_until=0
                WHERE question_id=?
                """,
                (answer_text[:50_000], now, question_id),
            )
            self._write_answer_events(question, question_id, answer_text, now)
            self._condition.notify_all()
            return self._question(question_id)

    def answer_question_web(
        self,
        question_id: str,
        *,
        actor_id: str | None,
        answer_text: str,
        tenant_id: str,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Human answer from the web UI.

        Only `pending` questions can be answered this way (a `claimed`
        question is being answered by a worker; web does not preempt it).
        `actor_id` is optional (web users have no actor); the principal
        check (or admin) is the authority here.
        """
        now = time.time()
        answer_text = answer_text.strip()
        if not answer_text:
            raise ValueError("answer_text is required")
        with self._condition, self._connection:
            question = self._question(question_id)
            if question["tenant_id"] != tenant_id:
                raise LookupError("question not found")
            if principal_id is not None and question["principal_id"] != principal_id:
                raise PermissionError("question does not belong to your principal")
            if question["status"] == "answered":
                return question
            if question["status"] != "pending":
                raise PermissionError(
                    "only pending questions can be answered from the web "
                    f"(current: {question['status']})"
                )
            if actor_id is not None:
                actor = self._actor(actor_id)
                if actor["tenant_id"] != tenant_id:
                    raise PermissionError("actor does not belong to tenant")
            self._connection.execute(
                """
                UPDATE hub_questions
                SET status='answered', answer_text=?, answered_at=?, lease_until=0
                WHERE question_id=?
                """,
                (answer_text[:50_000], now, question_id),
            )
            self._write_answer_events(question, question_id, answer_text, now)
            self._condition.notify_all()
            return self._question(question_id)

    def decline_question(
        self,
        question_id: str,
        *,
        actor_id: str | None,
        reason: str | None,
        tenant_id: str,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Human rejection from the web UI: pending/claimed -> declined."""
        now = time.time()
        with self._condition, self._connection:
            question = self._question(question_id)
            if question["tenant_id"] != tenant_id:
                raise LookupError("question not found")
            if principal_id is not None and question["principal_id"] != principal_id:
                raise PermissionError("question does not belong to your principal")
            if question["status"] in {"answered", "expired", "unsupported", "declined"}:
                return question
            if actor_id is not None:
                actor = self._actor(actor_id)
                if actor["tenant_id"] != tenant_id:
                    raise PermissionError("actor does not belong to tenant")
            self._connection.execute(
                """
                UPDATE hub_questions
                SET status='declined', lease_until=0, answered_at=?
                WHERE question_id=?
                """,
                (now, question_id),
            )
            # The rejection joins the shared memory so the asker side can
            # read it without polling the question table.
            self._connection.execute(
                """
                INSERT INTO hub_shared_events(
                    event_id, tenant_id, principal_id, scope, kind, session_id,
                    actor_id, node_id, payload_json, ttl_hours, expires_at, created_at
                ) VALUES (?, ?, ?, 'qa', 'answer', ?, ?, NULL, ?, 4320, ?, ?)
                """,
                (
                    f"answer_{uuid.uuid4().hex}",
                    question["tenant_id"],
                    question["principal_id"],
                    question["asker_session_id"],
                    question["asker_actor_id"],
                    _json(
                        {
                            "question_id": question_id,
                            "target_actor_id": question["target_actor_id"],
                            "asker_session_id": question["asker_session_id"],
                            "asker_task_id": question["asker_task_id"],
                            "question": question["message"],
                            "status": "declined",
                            "reason": (reason or "declined")[:10_000],
                            "answered_at": now,
                        }
                    ),
                    now + 4320 * 3600,
                    now,
                ),
            )
            self._condition.notify_all()
            return self._question(question_id)

    def _write_answer_events(
        self,
        question: dict[str, Any],
        question_id: str,
        answer_text: str,
        now: float,
    ) -> None:
        """Shared-memory answer entry + asker task event (both answer paths)."""
        # The answer joins the shared memory: future askers can read it
        # without asking again.
        self._connection.execute(
            """
            INSERT INTO hub_shared_events(
                event_id, tenant_id, principal_id, scope, kind, session_id,
                actor_id, node_id, payload_json, ttl_hours, expires_at, created_at
            ) VALUES (?, ?, ?, 'qa', 'answer', ?, ?, NULL, ?, 4320, ?, ?)
            """,
            (
                f"answer_{uuid.uuid4().hex}",
                question["tenant_id"],
                question["principal_id"],
                question["asker_session_id"],
                question["asker_actor_id"],
                _json(
                    {
                        "question_id": question_id,
                        "target_actor_id": question["target_actor_id"],
                        "asker_session_id": question["asker_session_id"],
                        "asker_task_id": question["asker_task_id"],
                        "question": question["message"],
                        "answer": answer_text[:10_000],
                        "answered_at": now,
                    }
                ),
                now + 4320 * 3600,
                now,
            ),
        )
        if question["asker_task_id"] is not None:
            # The asker's running task can observe the answer through its
            # own event stream without polling the question table. The
            # task may have ended already; then the answer still lives in
            # the shared event log above.
            task_row = self._connection.execute(
                "SELECT 1 FROM hub_tasks WHERE task_id=?",
                (question["asker_task_id"],),
            ).fetchone()
            if task_row is not None:
                self._event(
                    question["asker_task_id"],
                    "question.answered",
                    payload={
                        "question_id": question_id,
                        "answer": answer_text[:10_000],
                    },
                    tenant_id=question["tenant_id"],
                    now=now,
                )

    def list_questions(
        self,
        *,
        tenant_id: str,
        principal_id: str | None = None,
        actor_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        with self._lock:
            where = ["tenant_id=?"]
            params: list[Any] = [tenant_id]
            if principal_id is not None:
                where.append("principal_id=?")
                params.append(principal_id)
            if actor_id is not None:
                where.append("(asker_actor_id=? OR target_actor_id=?)")
                params.extend([actor_id, actor_id])
            if status is not None:
                where.append("status=?")
                params.append(status)
            params.append(limit)
            rows = self._connection.execute(
                f"""
                SELECT question_id FROM hub_questions
                WHERE {" AND ".join(where)}
                ORDER BY seq DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            return [self._question(str(row["question_id"])) for row in rows]

    def get_question(
        self, question_id: str, *, tenant_id: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            question = self._question(question_id)
            if tenant_id is not None and question["tenant_id"] != tenant_id:
                raise LookupError("question not found")
            return question

    def actor_online_node(self, actor_id: str, tenant_id: str) -> str | None:
        with self._lock:
            return self._actor_node_online(actor_id, tenant_id)

    def expire_questions(self) -> int:
        now = time.time()
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE hub_questions SET status='expired', lease_until=0
                WHERE status IN ('pending', 'claimed') AND created_at<=?
                """,
                (now - self.QUESTION_TTL_SECONDS,),
            )
            return int(cursor.rowcount or 0)

    def _actor_node_online(
        self, actor_id: str, tenant_id: str
    ) -> str | None:
        """The most recently seen node of an actor, if any."""
        row = self._connection.execute(
            """
            SELECT node_id FROM hub_nodes
            WHERE actor_id=? AND tenant_id=? AND last_seen_at>?
            ORDER BY last_seen_at DESC LIMIT 1
            """,
            (actor_id, tenant_id, time.time() - self._node_stale_seconds),
        ).fetchone()
        return str(row["node_id"]) if row is not None else None

    def _question(self, question_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_questions WHERE question_id=?", (question_id,)
        ).fetchone()
        if row is None:
            raise LookupError("question not found")
        return {
            "question_id": str(row["question_id"]),
            "seq": int(row["seq"]),
            "tenant_id": str(row["tenant_id"]),
            "principal_id": str(row["principal_id"]),
            "asker_actor_id": str(row["asker_actor_id"]),
            "asker_task_id": row["asker_task_id"],
            "asker_session_id": row["asker_session_id"],
            "target_actor_id": str(row["target_actor_id"]),
            "target_session_id": row["target_session_id"],
            "message": str(row["message"]),
            "require": row["require"],
            "status": str(row["status"]),
            "answer_text": row["answer_text"],
            "created_at": float(row["created_at"]),
            "answered_at": row["answered_at"],
        }
