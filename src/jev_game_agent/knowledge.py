"""Per-game Markdown retrieval and evidence-checked, journaled maintenance."""
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from jev_game_agent.exports import atomic_json
from jev_game_agent.store import encode, now


STOP_WORDS = set("a an the of to in on and or is are this that current visible simulated simulation click button close open wait observe page game unknown action next inspect before after condition conditions prerequisite prerequisites requirement requirements 点击 按钮 当前 页面 打开 关闭 操作 返回 确认 观察 游戏 未知 状态 等待 模拟 系统 路径 识别 范围 用途 入口 目标 说明 规则 结果 机制 条件 解锁 前置 核对 限制".split())


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def terms(text: str) -> set[str]:
    values = set()
    for word in re.findall(r"[A-Za-z_]+|[\u4e00-\u9fff]+", text.lower()):
        if word in STOP_WORDS:
            continue
        if re.fullmatch(r"[a-z_]+", word):
            values.add(word)
            values.update(part for part in word.split("_") if part and part not in STOP_WORDS)
            if word.endswith("s") and len(word) > 4:
                values.add(word[:-1])
        else:
            for size in (2, 3):
                values.update(word[index:index + size] for index in range(len(word) - size + 1)
                              if word[index:index + size] not in STOP_WORDS)
    return values


@dataclass
class SearchResult:
    items: list[dict] = field(default_factory=list)
    missing_refs: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def sections(text: str) -> list[dict]:
    headings = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, re.MULTILINE))
    title = headings[0][2] if headings else ""
    result = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[heading.end():end].strip()
        if not body or "修订记录" in heading[2] or len(heading[1]) == 1:
            continue
        match = re.search(r"[-*]\s*状态[:：]\s*([^\n]+)", body)
        label = match[1] if match else "historical_document"
        status = "disputed" if "有争议" in label else "outdated" if "已失效" in label else "hypothesis" if "推测" in label else "user_provided" if "用户提供" in label else "verified" if "已验证" in label else "observed" if "已观察" in label else "historical_document"
        result.append({"heading": heading[2], "title": title, "text": body, "status": status,
                       "start": heading.start(), "end": end, "level": len(heading[1])})
    return result


class KnowledgeError(Exception):
    pass


class KnowledgeLibrary:
    # Registered maintenance content, not arbitrary Markdown instructions interpreted as policy.
    POLICY_HASH = "1637d4b8a6ee82b4911b22ebb672e274e7065e35c325a40a2adab33ed6a3d5ae"

    def __init__(self, root: Path, work_root: Path):
        self.root = Path(root).resolve()
        self.work_root = Path(work_root).resolve()
        self._cache = {}

    def game_directory(self, game_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", game_id) or game_id == "unknown":
            raise KnowledgeError("Game identity must be resolved before using its knowledge")
        path = (self.root / game_id).resolve()
        if not path.is_relative_to(self.root):
            raise KnowledgeError("Knowledge path leaves its game namespace")
        return path

    def document_path(self, game_id: str, document: str) -> Path:
        if not re.fullmatch(r"systems/[A-Za-z0-9_-]+\.md", document) or Path(document).name.startswith("_"):
            raise KnowledgeError("Knowledge references must identify a system document")
        game = self.game_directory(game_id)
        path = (game / document).resolve()
        if not path.is_relative_to(game):
            raise KnowledgeError("Knowledge reference leaves the game namespace")
        return path

    def index(self, game_id: str) -> list[dict]:
        game = self.game_directory(game_id)
        files = sorted((game / "systems").glob("*.md")) if game.is_dir() else []
        contents = []
        for path in files:
            if path.name.startswith("_"):
                continue
            if not path.resolve().is_relative_to(game):
                raise KnowledgeError("A system document points outside its game namespace")
            raw = path.read_bytes()
            contents.append((path, raw, digest(raw)))
        fingerprint = [(str(path), hash_) for path, raw, hash_ in contents]
        cached = self._cache.get(game_id)
        if cached and cached[0] == fingerprint:
            return cached[1]
        entries = []
        for path, raw, hash_ in contents:
            document = path.relative_to(game).as_posix()
            for section in sections(raw.decode("utf-8-sig").replace("\r\n", "\n")):
                entries.append({"game_id": game_id, "ref": {"document": document, "heading": section["heading"]},
                                "document_hash": hash_, "text": section["text"], "status": section["status"],
                                "anchor_terms": terms(path.stem + " " + section["title"] + " " + section["heading"]),
                                "body_terms": terms(section["text"]), "source": "historical_game_knowledge_not_live_state"})
        self._cache[game_id] = (fingerprint, entries)
        return entries

    def search(self, game_id: str, observation, candidates) -> SearchResult:
        query = observation.knowledge_query
        required = [item.model_dump() for item in query.required_refs] if query else []
        if game_id == "unknown":
            return SearchResult(missing_refs=required, errors=["Game identity is unknown"] if query and (query.terms or required) else [])
        try:
            entries = self.index(game_id)
        except (KnowledgeError, OSError, UnicodeError) as exc:
            return SearchResult(errors=[str(exc) if isinstance(exc, KnowledgeError) else "Knowledge documents could not be read"])
        # Global task goals are deliberately absent: closing a notice must not load tower plans.
        source = [observation.page_label, *(item.label for item in observation.objects),
                  *(part for item in candidates for part in [item.semantic_target, item.description, *item.prerequisites]),
                  *observation.unknowns, *(query.terms if query else [])]
        query_terms = terms(" ".join(source))
        matches, missing = [], []
        for ref in required:
            exact = [item for item in entries if item["ref"] == ref]
            if len(exact) != 1:
                missing.append(ref)
        for entry in entries:
            anchor_match = query_terms.intersection(entry["anchor_terms"])
            is_required = entry["ref"] in required
            if not anchor_match and not is_required:
                continue
            score = len(anchor_match) * 4 + len(query_terms.intersection(entry["body_terms"]))
            matches.append((score, {key: value for key, value in entry.items() if key not in {"anchor_terms", "body_terms"}} |
                            {"required": is_required, "relevant": bool(anchor_match), "matched_terms": sorted(anchor_match)}))
        matches.sort(key=lambda pair: -pair[0])
        required_items = [item for score, item in matches if item["required"]]
        optional = [item for score, item in matches if not item["required"]][:6]
        return SearchResult(items=required_items + optional, missing_refs=missing)

    @contextmanager
    def locked(self, game_id: str):
        directory = self.work_root / "knowledge-locks"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (game_id + ".lock")
        with path.open("a+b") as handle:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + 2
            while True:
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise KnowledgeError("Knowledge maintenance is busy") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def check_policy(self, game_id: str) -> str:
        path = self.game_directory(game_id) / "maintenance.md"
        if not path.resolve().is_relative_to(self.game_directory(game_id)):
            raise KnowledgeError("Maintenance rules leave the game namespace")
        if not path.is_file():
            raise KnowledgeError("This game has no maintenance rules")
        content = path.read_text(encoding="utf-8-sig")
        if digest(content.encode()) != self.POLICY_HASH:
            raise KnowledgeError("Maintenance rules are new or changed; analyze and implement their policy before writing")
        return digest(content.encode())

    @staticmethod
    def portable_text(text: str) -> bool:
        return not re.search(r"(?i)(?:[a-z]:[/\\]|file://|screenshot_ref|TYPESAFE_API_KEY|Bearer\s|password\s*[:=]|token\s*[:=]|\brun/|\brun\\|\(\s*\d+\s*,\s*\d+\s*\))", text)

    @staticmethod
    def claim(section: dict) -> str:
        match = re.search(r"[-*]\s*结论[:：]\s*(.+?)(?=\n[-*]\s|\Z)", section["text"], re.DOTALL)
        return match[1].strip() if match else section["text"].strip()

    def write_job(self, game_id: str, key: str, changes: list[dict], evidence: dict) -> None:
        directory = self.work_root / "knowledge-journal" / game_id
        directory.mkdir(parents=True, exist_ok=True)
        job_path = directory / (digest(key.encode()) + ".json")
        job = {"game_id": game_id, "changes": changes, "evidence": evidence, "status": "prepared"}
        atomic_json(job_path, job)
        self.apply_job(job_path, job)

    def apply_job(self, job_path: Path, job: dict) -> None:
        game = self.game_directory(job["game_id"])
        paths = []
        # Precheck all files; on recovery either old or already-published bytes are valid.
        for change in job["changes"]:
            relative = change["document"]
            if relative != "questions.md":
                path = self.document_path(job["game_id"], relative)
            else:
                path = (game / relative).resolve()
                if not path.is_relative_to(game):
                    raise KnowledgeError("Questions path leaves the game namespace")
            current = path.read_bytes() if path.exists() else b""
            if digest(current) not in {change["before_hash"], digest(change["content"].encode())}:
                raise KnowledgeError("Knowledge changed during maintenance; preserve the edit and analyze")
            paths.append((path, change))
        for path, change in paths:
            current = path.read_bytes() if path.exists() else b""
            if digest(current) == digest(change["content"].encode()):
                continue
            if digest(current) != change["before_hash"]:
                raise KnowledgeError("Knowledge changed before publication; preserve the edit and analyze")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".jev.tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(change["content"])
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        job["status"] = "published"
        atomic_json(job_path, job)
        self._cache.pop(job["game_id"], None)

    def recover_jobs(self, game_id: str) -> None:
        directory = self.work_root / "knowledge-journal" / game_id
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job["status"] == "prepared":
                self.apply_job(path, job)

    def maintain(self, db, store, state, observation, drafts, conflicts, submission_key: str) -> list[dict]:
        if not drafts and not conflicts:
            return []
        issues = []
        game_id = state.get("identified_game_id", state["task"]["game_id"])
        if state["task"]["evidence_mode"] != "live":
            return [{"code": "non_live_evidence", "message": "Simulation or reconstruction cannot update shared knowledge"}]
        try:
            self.game_directory(game_id)
            with self.locked(game_id):
                policy_hash = self.check_policy(game_id)
                self.recover_jobs(game_id)
                for index, item in enumerate([*drafts, *conflicts]):
                    try:
                        issue = self.maintain_item(db, store, state, observation, item, game_id, policy_hash, submission_key + ":" + str(index))
                        if issue:
                            issues.append(issue)
                    except (KnowledgeError, OSError, UnicodeError) as exc:
                        issues.append({"code": "knowledge_not_written", "message": str(exc) if isinstance(exc, KnowledgeError) else "Knowledge storage unavailable", "ref": item.ref.model_dump()})
        except (KnowledgeError, OSError, ValueError) as exc:
            issues.append({"code": "maintenance_unavailable", "message": str(exc) if isinstance(exc, KnowledgeError) else "Knowledge maintenance could not be recovered"})
        return issues

    def maintain_item(self, db, store, state, observation, item, game_id, policy_hash, key):
        from jev_game_agent.contracts import KnowledgeDraft
        item_hash = digest(encode(item.model_dump(mode="json")).encode())
        prior_path = self.work_root / "knowledge-journal" / game_id / (digest(key.encode()) + ".json")
        if prior_path.exists():
            prior = json.loads(prior_path.read_text(encoding="utf-8"))
            if prior["evidence"].get("item_hash") != item_hash:
                raise KnowledgeError("Knowledge journal key was reused with different content")
            if prior["status"] != "published":
                raise KnowledgeError("Knowledge journal must be recovered before retrying")
            for change in prior["changes"]:
                target = self.game_directory(game_id) / change["document"]
                if digest(target.read_bytes()) != digest(change["content"].encode()):
                    raise KnowledgeError("Published knowledge was subsequently edited; reread before retrying")
            store.event(db, "knowledge_publication_recovered", {"ref": item.ref.model_dump(), "original_evidence": prior["evidence"]})
            conflict_job = len(prior["changes"]) > 1 and (not isinstance(item, KnowledgeDraft) or item.operation != "replace")
            return {"code": "knowledge_conflict", "ref": item.ref.model_dump(), "message": "Recovered conflict still needs analysis"} if conflict_job else None
        path = self.document_path(game_id, item.ref.document)
        if re.fullmatch(r"K\d+", item.ref.heading, re.IGNORECASE):
            raise KnowledgeError("Knowledge headings must describe the conclusion rather than use entry IDs")
        raw = path.read_bytes() if path.exists() else b""
        if digest(raw) != item.expected_document_hash:
            raise KnowledgeError("Knowledge document hash is stale; reread before revising")
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
        existing = [part for part in sections(text) if part["heading"] == item.ref.heading]
        if len(existing) > 1:
            raise KnowledgeError("The knowledge heading is ambiguous")
        is_draft = isinstance(item, KnowledgeDraft)
        evidence = {"run_id": state["run_id"], "observation_id": state["observation_id"],
                    "facts": [observation.facts[index] for index in item.fact_indices], "policy_hash": policy_hash, "item_hash": item_hash}
        if is_draft:
            if not item.reusable or not item.sensitive_content_removed or item.contains_account_progress:
                raise KnowledgeError("Draft has not passed reusable-knowledge and sensitive-data checks")
            if item.admission_reason == "main_story" and item.ref.document != "systems/onboarding.md":
                raise KnowledgeError("Main-story knowledge belongs in onboarding.md")
            if item.evidence_kind == "verified_result":
                match = db.execute("SELECT data FROM events WHERE kind='visual_verification' ORDER BY seq DESC LIMIT 1").fetchone()
                verified = json.loads(match[0]) if match else None
                if not verified or verified["observation_id"] != state["observation_id"] or verified["outcome"] != "success":
                    raise KnowledgeError("Draft requires a current visually verified result, not a Jev choice")
            content = encode(item.model_dump(mode="json", exclude={"ref", "fact_indices", "expected_document_hash"}))
            if not self.portable_text(content):
                raise KnowledgeError("Shared knowledge cannot contain local evidence paths, coordinates or credentials")
            if item.operation == "append" and any(re.sub(r"\s+", "", self.claim(part)) == re.sub(r"\s+", "", item.statement) for part in sections(text)):
                store.event(db, "knowledge_duplicate", {"ref": item.ref.model_dump()})
                return None
        elif not self.portable_text(item.summary):
            raise KnowledgeError("Conflict description must remain portable and nonsensitive")
        dispute = not is_draft or item.operation == "dispute" or (existing and item.operation != "replace")
        if is_draft and item.operation == "replace":
            if not existing or not item.resolution_explanation or item.admission_reason != "correction":
                raise KnowledgeError("Replacement needs an existing entry, correction reason and explicit analysis")
        if dispute:
            if not existing:
                raise KnowledgeError("Conflict must reference an existing entry")
            part = existing[0]
            body = part["text"]
            if re.search(r"[-*]\s*状态[:：]", body):
                body = re.sub(r"([-*]\s*状态[:：]\s*)[^\n]+", r"\1有争议。", body, count=1)
            else:
                body = "- 状态：有争议。\n" + body
            replacement = "#" * part["level"] + " " + part["heading"] + "\n\n" + body + "\n\n"
            updated = text[:part["start"]] + replacement + text[part["end"]:]
            summary = item.statement if is_draft else item.summary
            old_claim = self.claim(part)
            if not self.portable_text(old_claim):
                raise KnowledgeError("Existing conflict evidence must be made portable before publication")
            question_path = self.game_directory(game_id) / "questions.md"
            if not question_path.resolve().is_relative_to(self.game_directory(game_id)):
                raise KnowledgeError("Questions path leaves its namespace")
            questions_raw = question_path.read_bytes() if question_path.exists() else b""
            questions = questions_raw.decode("utf-8-sig").replace("\r\n", "\n")
            anchor = quote(item.ref.heading.lower().replace(" ", "-"))
            questions += f"\n## {item.ref.heading}的规则冲突\n\n- 原条目：[{item.ref.heading}]({item.ref.document}#{anchor})\n- 旧结论：{old_claim}\n- 新观察或解释：{summary}\n- 状态：有争议，等待区分适用范围并分析。\n"
            changes = [{"document": item.ref.document, "before_hash": digest(raw), "content": updated},
                       {"document": "questions.md", "before_hash": digest(questions_raw), "content": questions}]
            self.write_job(game_id, key, changes, evidence)
            store.event(db, "knowledge_disputed", {"ref": item.ref.model_dump(), **evidence})
            return {"code": "knowledge_conflict", "ref": item.ref.model_dump(), "message": "Existing knowledge was marked disputed; analyze before selecting a replacement"}
        label = "已验证" if item.status == "verified" else "已观察"
        scope = item.scope
        entry = (f"### {item.ref.heading}\n\n- 状态：{label}。\n- 结论：{item.statement}\n"
                 f"- 界面判断点：{item.visible_checks}\n- 适用范围：环境={scope.environment}；构建={scope.build}；账号阶段={scope.account_stage}；前置={scope.prerequisites}；特权={scope.privileges}。\n"
                 f"- 最后核实日期：{datetime.now(timezone(timedelta(hours=8))).date().isoformat()}。\n")
        if item.operation == "replace":
            part = existing[0]
            entry = entry.replace("### ", "#" * part["level"] + " ", 1)
            entry += f"- 修订说明：{item.resolution_explanation}\n"
            updated = text[:part["start"]] + entry + "\n" + text[part["end"]:]
        else:
            if not text:
                text = ("# " + Path(item.ref.document).stem + "\n\n"
                        "## 系统名称与用途\n\n未知，需在目标范围内观察。\n\n"
                        "## 入口与识别\n\n尚未核实的入口与解锁条件均为未知。\n\n"
                        "## 可复用操作路径\n\n完整路径尚未走通，不预设操作步骤。\n\n"
                        "## 操作与系统关系\n\n未核实的成本、限制与依赖均为未知。\n\n"
                        "## 知识条目\n\n## 待解问题\n\n参见 [待解问题](../questions.md)。\n\n## 修订记录\n")
                # Place the first entry in the template's knowledge section.
                text = text.replace("## 知识条目\n\n", "## 知识条目\n\n" + entry + "\n", 1)
                entry = ""
            revision = re.search(r"^## 修订记录", text, re.MULTILINE)
            cut = revision.start() if revision else len(text)
            updated = text[:cut].rstrip() + "\n\n" + entry + "\n" + text[cut:]
        date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        revision = f"- {date}：{item.ref.heading}；{item.increment_reason}\n"
        if "## 修订记录" not in updated:
            updated = updated.rstrip() + "\n\n## 修订记录\n"
        updated = updated.rstrip() + "\n\n" + revision
        changes = [{"document": item.ref.document, "before_hash": digest(raw), "content": updated}]
        if item.operation == "replace":
            question_path = self.game_directory(game_id) / "questions.md"
            if question_path.exists():
                if not question_path.resolve().is_relative_to(self.game_directory(game_id)):
                    raise KnowledgeError("Questions path leaves its namespace")
                questions_raw = question_path.read_bytes()
                questions = questions_raw.decode("utf-8-sig").replace("\r\n", "\n")
                marker = f"## {item.ref.heading}的规则冲突"
                # Resolve only the explicitly identified conflict, retaining its opposing evidence.
                match = re.search(re.escape(marker) + r"\n.*?(?=\n## |\Z)", questions, re.DOTALL)
                if match:
                    resolved = re.sub(r"- 状态：[^\n]+", f"- 状态：已分析修订；参见 {item.ref.document} / {item.ref.heading}。", match[0])
                    questions = questions[:match.start()] + resolved + questions[match.end():]
                    changes.append({"document": "questions.md", "before_hash": digest(questions_raw), "content": questions})
        self.write_job(game_id, key, changes, evidence)
        store.event(db, "knowledge_written", {"ref": item.ref.model_dump(), "operation": item.operation, **evidence})
        return None
