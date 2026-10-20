from __future__ import annotations

import hashlib
from typing import Any

from sentinel.assess.chunker import Chunker
from sentinel.skills.loader import Skills


class RetrievalPackager:
    """Build bounded evidence bundles for reasoning skills."""

    _SKILL_KEYWORDS: dict[str, tuple[str, ...]] = {
        Skills.QUICK_REVIEW: ("todo", "fixme", "error", "warning", "exception"),
        Skills.SECURITY: ("auth", "token", "password", "secret", "eval", "exec", "sql"),
        Skills.CORRECTNESS: ("if", "else", "return", "raise", "assert", "null", "none"),
        Skills.DATA_INTEGRITY: ("transaction", "commit", "rollback", "schema", "integrity"),
    }

    def __init__(self, config: Any) -> None:
        self._config = config
        # Narrower than review chunking by default to reduce prompt size risk.
        max_lines = min(getattr(config, "chunk_max_lines", 400), 120)
        overlap = min(getattr(config, "chunk_overlap_lines", 40), 20)
        if overlap >= max_lines:
            overlap = max(0, max_lines - 1)
        self._chunker = Chunker(max_lines=max_lines, overlap_lines=overlap)

    def build_module_manifest(self, module: dict[str, Any], source_code: str) -> dict[str, Any]:
        module_path = str(module.get("path", ""))
        module_name = module.get("name", module_path or "unknown")
        chunks = self._chunker.chunk_string(source_code, file_path=module_path or module_name)

        skill_entries = []
        for skill, keywords in self._SKILL_KEYWORDS.items():
            snippets = self._select_snippets(module_path, chunks, keywords)
            skill_entries.append(
                {
                    "skill": skill,
                    "snippets": snippets,
                }
            )

        return {
            "module": module_name,
            "module_path": module_path,
            "policy": {
                "max_snippets_per_skill": self._config.retrieval_max_snippets_per_skill,
                "max_chars_per_snippet": self._config.retrieval_max_chars_per_snippet,
            },
            "skills": skill_entries,
        }

    def render_reasoning_source(self, module_manifest: dict[str, Any]) -> str:
        lines: list[str] = []
        module_path = module_manifest.get("module_path", "")
        lines.append(f"# retrieval-manifest module={module_path}")

        for skill_entry in module_manifest.get("skills", []):
            skill = skill_entry.get("skill", "unknown-skill")
            lines.append(f"\n## skill: {skill}")
            for snippet in skill_entry.get("snippets", []):
                lines.append(
                    "\n".join(
                        [
                            (
                                f"# snippet path={snippet.get('module_path','')} "
                                f"lines={snippet.get('start_line', 0)}-{snippet.get('end_line', 0)} "
                                f"hash={snippet.get('snippet_hash', '')}"
                            ),
                            snippet.get("content", ""),
                        ]
                    )
                )

        return "\n".join(lines)

    def _select_snippets(
        self,
        module_path: str,
        chunks: list[Any],
        keywords: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        ranked: list[tuple[int, Any]] = []
        for index, chunk in enumerate(chunks):
            text = chunk.content.lower()
            score = sum(text.count(k) for k in keywords)
            ranked.append((score, index, chunk))

        ranked.sort(key=lambda item: (-item[0], item[1]))
        limit = self._config.retrieval_max_snippets_per_skill
        positive_ranked = [(score, chunk) for score, _, chunk in ranked if score > 0]
        selected = positive_ranked[:limit] if positive_ranked else [(score, chunk) for score, _, chunk in ranked[:limit]]

        snippets: list[dict[str, Any]] = []
        for _, chunk in selected:
            content = chunk.content[: self._config.retrieval_max_chars_per_snippet]
            snippets.append(
                {
                    "module_path": module_path,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "char_count": len(content),
                    "snippet_hash": self._hash(content),
                    "content": content,
                }
            )

        if not snippets and chunks:
            first = chunks[0]
            content = first.content[: self._config.retrieval_max_chars_per_snippet]
            snippets.append(
                {
                    "module_path": module_path,
                    "start_line": first.start_line,
                    "end_line": first.end_line,
                    "char_count": len(content),
                    "snippet_hash": self._hash(content),
                    "content": content,
                }
            )

        return snippets

    def _hash(self, text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
