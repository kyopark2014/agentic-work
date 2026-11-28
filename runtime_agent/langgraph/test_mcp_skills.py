"""Unit tests for SEP-2640 host verification (lazy load / digest / frontmatter)."""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_skills  # noqa: E402


def _skill_md(name: str = "use-vault", description: str = "vault notes") -> bytes:
    text = (
        f"---\nname: {name}\ndescription: {description}\n---\n\n"
        f"# {name}\n\nbody\n"
    )
    return text.encode("utf-8")


class VerifySkillMdBytesTest(unittest.TestCase):
    def test_ok_with_manifest(self):
        raw = _skill_md()
        uri = "skill://use-vault/SKILL.md"
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        resources = [{"uri": uri, "digest": digest, "size": len(raw)}]
        fm, body = mcp_skills.verify_skill_md_bytes(
            raw,
            uri=uri,
            resources=resources,
            expected_frontmatter={"name": "use-vault", "description": "vault notes"},
        )
        self.assertEqual(fm["name"], "use-vault")
        self.assertIn("body", body)

    def test_digest_mismatch_rejected(self):
        raw = _skill_md()
        uri = "skill://use-vault/SKILL.md"
        resources = [
            {
                "uri": uri,
                "digest": "sha256:" + ("0" * 64),
                "size": len(raw),
            }
        ]
        with self.assertRaises(ValueError) as ctx:
            mcp_skills.verify_skill_md_bytes(
                raw,
                uri=uri,
                resources=resources,
                expected_frontmatter={"name": "use-vault", "description": "vault notes"},
            )
        self.assertIn("digest mismatch", str(ctx.exception))

    def test_frontmatter_mismatch_rejected(self):
        raw = _skill_md(description="changed")
        uri = "skill://use-vault/SKILL.md"
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        resources = [{"uri": uri, "digest": digest, "size": len(raw)}]
        with self.assertRaises(ValueError) as ctx:
            mcp_skills.verify_skill_md_bytes(
                raw,
                uri=uri,
                resources=resources,
                expected_frontmatter={"name": "use-vault", "description": "vault notes"},
            )
        self.assertIn("frontmatter mismatch", str(ctx.exception))

    def test_size_mismatch_rejected(self):
        raw = _skill_md()
        uri = "skill://use-vault/SKILL.md"
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        resources = [{"uri": uri, "digest": digest, "size": len(raw) - 1}]
        with self.assertRaises(ValueError) as ctx:
            mcp_skills.verify_skill_md_bytes(
                raw,
                uri=uri,
                resources=resources,
                expected_frontmatter={"name": "use-vault", "description": "vault notes"},
            )
        self.assertIn("size mismatch", str(ctx.exception))


class RegisterMcpSkillsLazyTest(unittest.TestCase):
    def test_register_defers_body(self):
        import skill

        # Isolate registry
        skill.skill_managers.pop("base", None)
        entries = [
            {
                "name": "use-vault",
                "description": "vault notes",
                "instructions": "",
                "uri": "skill://use-vault/SKILL.md",
                "server": "use-vault",
                "origin": "mcp:use-vault",
                "path": "mcp://use-vault/use-vault",
                "connection": {"transport": "http", "url": "http://example.invalid"},
                "frontmatter": {"name": "use-vault", "description": "vault notes"},
                "resources": [
                    {
                        "uri": "skill://use-vault/SKILL.md",
                        "digest": "sha256:" + ("a" * 64),
                        "size": 10,
                    }
                ],
            }
        ]
        names = skill.register_mcp_skills(entries)
        self.assertEqual(names, ["use-vault"])
        sm = skill.skill_managers["base"]
        registered = sm.get_skill("use-vault")
        self.assertIsNotNone(registered)
        self.assertEqual(registered.instructions, "")
        self.assertEqual(registered.uri, "skill://use-vault/SKILL.md")
        self.assertIsInstance(registered.connection, dict)


if __name__ == "__main__":
    unittest.main()
