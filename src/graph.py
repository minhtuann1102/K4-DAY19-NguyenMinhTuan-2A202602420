"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Two ontologies live in this file; the env var KG_ONTOLOGY picks one at call time:

    KG_ONTOLOGY=own   (default) — own design, see report/ONTOLOGY.md
    KG_ONTOLOGY=hint            — the suggested ontology of the lab, kept verbatim as the baseline
                                  that produced ket_qua_benchmark_kg.hint.txt

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)

Own ontology (differences marked *):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text, doc_id, *severity, *is_max_clause})
    (:Clause)-[:MENTIONS {*point, *min_g, *max_g}]->(:Substance {name})        weight thresholds per điểm
    *(:Substance)-[:IS_A]->(:Substance)                     Ketamine -> "chất ma túy khác (thể rắn)"
    *(:NewsArticle {doc_id, name, url})-[:REPORTS]->(:Case {*id, name, summary, date})
    (:Case)-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount, *amount_g}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def ontology() -> str:
    """'own' (default) or 'hint' — read at call time so one process can build and query consistently."""
    return "hint" if os.getenv("KG_ONTOLOGY", "own").strip().lower() == "hint" else "own"

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    if not name or not name.strip():
        return None
    norm_name = normalize(name)
    if not norm_name:
        return None

    norm_to_orig: dict[str, str] = {}
    for orig in known:
        nk = normalize(orig)
        if nk not in norm_to_orig:
            norm_to_orig[nk] = orig

    if norm_name in norm_to_orig:
        return norm_to_orig[norm_name]

    matches = difflib.get_close_matches(norm_name, list(norm_to_orig.keys()), n=1, cutoff=0.8)
    if matches:
        return norm_to_orig[matches[0]]

    return None

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))

def _clause_fact(article_id: str, title: str, number: int, penalty: str, notes: list[str]) -> str:
    label = f" ({'; '.join(notes)})" if notes else ""
    return f"[{article_id} - {title}] khoản {number}{label}: {penalty}"

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers (baseline, KG_ONTOLOGY=hint)
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# OWN ontology: extraction helpers (default, KG_ONTOLOGY=own)
# ----------------------------------------------------------------------------------------------

# Street names -> canonical substance (matched as whole words, case-insensitive).
SUBSTANCE_SYNONYMS: dict[str, str] = {
    "thuốc lắc": "MDMA",
    "kẹo": "MDMA",
    "ecstasy": "MDMA",
    "đá": "Methamphetamine",
    "ma túy đá": "Methamphetamine",
    "hàng đá": "Methamphetamine",
    "hồng phiến": "Methamphetamine",
    "khay": "Ketamine",
    "hàng khay": "Ketamine",
    "cỏ mỹ": "cần sa",
    "bồ đà": "cần sa",
}
# Words that name no specific substance: no Substance node is created for them.
GENERIC_SUBSTANCES = {"ma túy", "ma tuý", "chất ma túy", "chất ma tuý", "ma túy tổng hợp", "ma tuý tổng hợp",
                      "chất cấm", "ma túy các loại"}
# BLHS has no clause naming these; they fall under "Các chất ma túy khác ở thể rắn".
OTHER_SOLID = "chất ma túy khác (thể rắn)"
SUBSTANCE_GROUPS = {"Ketamine": OTHER_SOLID}
SUSPECT_ROLES = ["bị cáo", "bị can", "nghi phạm"]

POINT = re.compile(r"^([a-zđ])\)\s*(.+)$", re.MULTILINE)
WEIGHT = r"([\d.,]+)\s*(gam|kilôgam)"
RANGE = re.compile(r"từ\s+" + WEIGHT + r"\s+đến\s+dưới\s+" + WEIGHT)
OPEN_RANGE = re.compile(WEIGHT + r"\s+trở\s+lên")
AMOUNT = re.compile(r"(\d+(?:[.,]\d+)*)\s*(tấn|kg|kilôgam|kilogam|gam|gram|gr|g|mg|miligam)(?!\w)")

def _word_in(term: str, lowered: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(term.lower()) + r"(?!\w)", lowered) is not None

def _to_float(number: str) -> float:
    """Vietnamese numbers: '9,6' -> 9.6, '1.000' -> 1000, '1.234,5' -> 1234.5, '05' -> 5."""
    if "," in number and "." in number:
        number = number.replace(".", "").replace(",", ".")
    elif "," in number:
        number = number.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", number):
        number = number.replace(".", "")
    return float(number)

def parse_amount_g(amount: str) -> float | None:
    """'hơn 9,6kg' -> 9600.0, 'gần 406g' -> 406.0, '5 viên' -> None."""
    match = AMOUNT.search(amount.lower())
    if not match:
        return None
    factor = {"tấn": 1_000_000, "kg": 1000, "kilôgam": 1000, "kilogam": 1000,
              "mg": 0.001, "miligam": 0.001}.get(match.group(2), 1)
    return round(_to_float(match.group(1)) * factor, 3)

def find_canonical_substances(text: str) -> list[str]:
    """Canonical names (+ the 'other solid' group) mentioned in text, matched as whole words."""
    lowered = text.lower()
    found = {name for name in SUBSTANCES if _word_in(name, lowered)}
    found |= {canon for syn, canon in SUBSTANCE_SYNONYMS.items() if _word_in(syn, lowered)}
    if "chất ma túy khác ở thể rắn" in lowered:
        found.add(OTHER_SOLID)
    return sorted(found)

def canonical_substance(name: str) -> str | None:
    """One LLM-written substance name -> canonical node name, or None for generic words like 'ma túy'."""
    lowered = re.sub(r"\s+", " ", name.strip().lower())
    if not lowered or lowered in GENERIC_SUBSTANCES:
        return None
    if lowered in SUBSTANCE_SYNONYMS:
        return SUBSTANCE_SYNONYMS[lowered]
    found = [s for s in find_canonical_substances(lowered) if s != OTHER_SOLID]
    if len(found) == 1:
        return found[0]
    return link_entity(lowered, SUBSTANCES, normalize=lambda s: s.strip().lower()) or lowered

def penalty_severity(penalty: str) -> float:
    """Comparable weight of a clause's main penalty: tử hình > chung thân > max years of prison > no prison."""
    lowered = penalty.lower()
    if "tử hình" in lowered:
        return 100.0
    if "chung thân" in lowered:
        return 50.0
    if re.search(r"(?<!\w)tù(?!\w)", lowered):
        years = [float(y) for y in re.findall(r"(\d+)\s*năm", lowered)]
        months = [float(m) / 12 for m in re.findall(r"(\d+)\s*tháng", lowered)]
        return max(years + months, default=0.0)
    return 0.0

def _clause_mentions(text: str) -> list[dict]:
    """Substances per điểm, with the weight range of that điểm when the law gives one (grams)."""
    first_line = text.splitlines()[0]
    lines = [("", first_line)] + [(m.group(1), m.group(2)) for m in POINT.finditer(text)]
    mentions: dict[tuple[str, str], dict] = {}
    for point, line in lines:
        min_g = max_g = None
        if (r := RANGE.search(line)):
            min_g = _to_float(r.group(1)) * (1000 if r.group(2) == "kilôgam" else 1)
            max_g = _to_float(r.group(3)) * (1000 if r.group(4) == "kilôgam" else 1)
        elif (o := OPEN_RANGE.search(line)):
            min_g = _to_float(o.group(1)) * (1000 if o.group(2) == "kilôgam" else 1)
        for substance in find_canonical_substances(line):
            mentions[(substance, point)] = {"substance": substance, "point": point, "min_g": min_g, "max_g": max_g}
    return list(mentions.values())

def parse_law_article_own(doc: Document) -> dict[str, Any]:
    """parse_law_article + per-điểm weight thresholds + a severity rank that marks the real top clause."""
    article = parse_law_article(doc)
    for clause in article["clauses"]:
        clause["severity"] = penalty_severity(clause["penalty"])
        clause["mentions"] = _clause_mentions(clause["text"])
        del clause["substances"]
    top = max((c["severity"] for c in article["clauses"]), default=0.0)
    for clause in article["clauses"]:
        clause["is_max_clause"] = top > 0 and clause["severity"] == top
    return article

NEWS_EXTRACTION_PROMPT_OWN = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin trong NỘI DUNG CHÍNH của bài. Cuối bài thường có một đoạn ngắn giới thiệu một bài báo KHÁC
(tin liên quan, nhân vật và vụ việc khác hẳn nội dung chính): BỎ QUA đoạn đó, không tạo vụ việc từ nó.
Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc để trống",
  "location": "tỉnh/thành phố, để trống nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất cụ thể, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp; bỏ qua nếu bài chỉ nói chung chung 'ma túy'",
                   "amount": "khối lượng kèm đơn vị đúng như bài viết, ví dụ: hơn 9,6kg, gần 406g; để trống nếu không có"}}],
  "people": [{{"name": "họ tên đầy đủ", "aliases": ["biệt danh, tên tài khoản mạng xã hội"],
               "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của RIÊNG người này (từ DANH SÁCH TỘI DANH) hoặc để trống",
               "sentence": "mức án nếu đã tuyên, ví dụ: tử hình, 36 tháng tù; để trống nếu chưa xét xử"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị, hỏi đáp pháp luật...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases_own(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction; crimes re-linked to the law KB, substances canonicalised, amounts parsed to grams."""
    prompt = NEWS_EXTRACTION_PROMPT_OWN.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = [c for c in json.loads(llm_fn(prompt)).get("cases", []) if isinstance(c, dict)]
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        for key in ("name", "summary", "date", "location"):
            case[key] = _clean(case.get(key))
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges") or []) if c})
        substances: dict[str, dict] = {}
        for s in case.get("substances") or []:
            if not isinstance(s, dict):
                continue
            name = canonical_substance(_clean(s.get("name")))
            if not name:
                continue
            amount = _clean(s.get("amount"))
            grams = parse_amount_g(amount)
            if name not in substances or (grams or 0) > (substances[name]["amount_g"] or 0):
                substances[name] = {"name": name, "amount": amount, "amount_g": grams}
        case["substances"] = list(substances.values())
        people = []
        for person in case.get("people") or []:
            if not isinstance(person, dict):
                continue
            name = _clean(person.get("name"))
            if not name:
                continue
            aliases = person.get("aliases") or []
            aliases = [aliases] if isinstance(aliases, str) else aliases
            people.append({
                "name": name,
                "aliases": _dedupe([a for a in map(_clean, aliases) if a and a != name]),
                "role": _clean(person.get("role")),
                "charge": link_entity(_clean(person.get("charge")), known_crimes) or "",
                "sentence": _clean(person.get("sentence")),
            })
        case["people"] = people
    return cases

PLACEHOLDERS = {"chuỗi rỗng", "để trống", "trống", "không có", "không rõ", "chưa có", "chưa rõ", "không",
                "n/a", "na", "null", "none", "unknown", "-"}

def _clean(value: Any) -> str:
    """LLM field -> stripped text; placeholders such as 'chuỗi rỗng' or 'không rõ' become ''."""
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip("\"'").strip()
    return "" if text.lower() in PLACEHOLDERS else text

def _is_full_name(name: str) -> bool:
    """Good enough to identify someone across articles: 2+ words, no digits ('Phương', '7 công dân' are not)."""
    return len(name.split()) >= 2 and not any(ch.isdigit() for ch in name)

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- OWN ontology: writes

    def own_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Substance", "name"),
                           ("NewsArticle", "doc_id"), ("Case", "id"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article_own(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id,
                  cl.severity = clause.severity, cl.is_max_clause = clause.is_max_clause
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (m IN clause.mentions | MERGE (sub:Substance {name: m.substance})
                MERGE (cl)-[r:MENTIONS {point: m.point}]->(sub) SET r.min_g = m.min_g, r.max_g = m.max_g)
            """,
            **article,
        )

    def add_substance_groups(self) -> None:
        for member, group in SUBSTANCE_GROUPS.items():
            self.run("MERGE (m:Substance {name: $m}) MERGE (g:Substance {name: $g}) MERGE (m)-[:IS_A]->(g)",
                     m=member, g=group)

    def _resolve_person(self, person: dict) -> tuple[str, list[str]]:
        """Same human across articles: match on full name or a 2+ word alias. Returns (node name, aliases)."""
        name = person["name"]
        keys = [a for a in person["aliases"] if _is_full_name(a)]
        if _is_full_name(name):
            keys.append(name)
        rows = self.run(
            """
            MATCH (p:Person)
            WHERE p.name = $name OR p.name IN $keys OR any(a IN coalesce(p.aliases, []) WHERE a IN $keys)
            RETURN p.name AS name, coalesce(p.aliases, []) AS aliases LIMIT 1
            """,
            name=name, keys=keys,
        )
        canonical = rows[0]["name"] if rows else name
        aliases = (rows[0]["aliases"] if rows else []) + person["aliases"] + ([name] if name != canonical else [])
        return canonical, [a for a in _dedupe(aliases) if a != canonical]

    def _resolve_case_id(self, case: dict, suspects: list[str], doc: Document, index: int) -> str:
        """Same case across articles = shares a named suspect (or the exact name); else a new per-article id."""
        rows = self.run(
            """
            MATCH (p:Person)-[r:INVOLVED_IN]->(k:Case)
            WHERE p.name IN $suspects AND r.role IN $roles
            RETURN k.id AS id, count(DISTINCT p) AS shared ORDER BY shared DESC, id LIMIT 1
            """,
            suspects=suspects, roles=SUSPECT_ROLES,
        )
        if not rows:
            rows = self.run("MATCH (k:Case {name: $name}) RETURN k.id AS id LIMIT 1", name=case.get("name") or "")
        return rows[0]["id"] if rows else f"{doc.id}#{index}"

    def add_news_article_own(self, doc: Document, cases: list[dict]) -> None:
        title = doc.metadata.get("title", "")
        self.run("MERGE (n:NewsArticle {doc_id: $doc_id}) SET n.name = $title, n.url = $url",
                 doc_id=doc.id, title=title, url=doc.metadata.get("source_url", ""))
        for index, case in enumerate(cases):
            people = []
            for person in case.get("people", []):
                canonical, aliases = self._resolve_person(person)
                people.append({**person, "name": canonical, "aliases": aliases})
            suspects = [p["name"] for p in people if p["role"] in SUSPECT_ROLES and _is_full_name(p["name"])]
            case_id = self._resolve_case_id(case, suspects, doc, index)
            self.run(
                """
                MERGE (k:Case {id: $case_id})
                  ON CREATE SET k.name = $name, k.summary = $summary, k.date = $date
                WITH k
                MATCH (n:NewsArticle {doc_id: $doc_id})
                MERGE (n)-[:REPORTS]->(k)
                FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                    MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
                FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
                FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                    SET r.amount = CASE WHEN r.amount IS NULL OR (s.amount_g IS NOT NULL
                                        AND (r.amount_g IS NULL OR s.amount_g > r.amount_g)) THEN s.amount ELSE r.amount END,
                        r.amount_g = CASE WHEN s.amount_g IS NOT NULL
                                          AND (r.amount_g IS NULL OR s.amount_g > r.amount_g) THEN s.amount_g ELSE r.amount_g END)
                FOREACH (p IN $people | MERGE (person:Person {name: p.name}) SET person.aliases = p.aliases
                    MERGE (person)-[r:INVOLVED_IN]->(k)
                    SET r.role = CASE WHEN p.role <> '' THEN p.role ELSE coalesce(r.role, '') END,
                        r.charge = CASE WHEN p.charge <> '' THEN p.charge ELSE coalesce(r.charge, '') END,
                        r.sentence = CASE WHEN p.sentence <> '' THEN p.sentence ELSE coalesce(r.sentence, '') END)
                """,
                case_id=case_id, name=case.get("name") or title or doc.id, summary=case.get("summary", ""),
                date=case.get("date", ""), location=(case.get("location") or "").strip(), doc_id=doc.id,
                charges=sorted(set(case.get("charges", [])) | {p["charge"] for p in people if p["charge"]}),
                substances=case.get("substances", []), people=people,
            )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + 1 hop, then the legal basis of every case reached.

        Multi-hop facts come first and generic 1-hop seed edges last, so truncation drops the generic ones.
        """
        if ontology() == "hint":
            return self._context_hint(question, doc_ids, max_facts)
        return self._context_own(question, doc_ids, max_facts)

    def _context_hint(self, question: str, doc_ids: list[str], max_facts: int) -> list[str]:
        seed_ids, seed_edges = self.seed_facts(question, doc_ids)
        facts = []
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN DISTINCT k.name AS name, k.summary AS summary
            """,
            ids=seed_ids,
        )
        for c in cases:
            facts.append(f"Vụ việc '{c['name']}': {c['summary']}" if c.get("summary") else f"Vụ việc: {c['name']}")
        clauses = self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE (elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids })
              AND (cl.number = 1 OR EXISTS { MATCH (k)-[:INVOLVES]->(:Substance)<-[:MENTIONS]-(cl) })
            RETURN DISTINCT a.id AS article_id, a.title AS title, cl.number AS number, cl.penalty AS penalty, cl.text AS text
            ORDER BY article_id, number
            """,
            ids=seed_ids,
        )
        article_numbers = re.findall(r"[Đđ]iều\s+(\d+)", question)
        if article_numbers:
            clauses += self.run(
                """
                MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
                WHERE a.id IN $art_ids
                  AND (cl.number = 1 OR any(s IN $subs WHERE EXISTS { MATCH (cl)-[:MENTIONS]->(:Substance {name: s}) }))
                RETURN DISTINCT a.id AS article_id, a.title AS title, cl.number AS number, cl.penalty AS penalty, cl.text AS text
                ORDER BY article_id, number
                """,
                art_ids=[f"Điều {n} BLHS" for n in article_numbers], subs=find_substances(question),
            )
        for r in clauses:
            facts.append(_clause_fact(r["article_id"], r["title"], r["number"], r["penalty"] or r["text"], []))
        return _dedupe(facts + seed_edges)[:max_facts]

    def _context_own(self, question: str, doc_ids: list[str], max_facts: int) -> list[str]:
        # Clause edges (MENTIONS/HAS_CLAUSE) are noise as 1-hop facts: law facts are added below on purpose.
        seed_ids, seed_edges = self.seed_facts(question, doc_ids, skip_labels=("Clause",))
        facts: list[str] = []
        law_pairs: list[dict] = []          # (case, article) whose clauses go into the prompt

        # 1. People named in the question -> THEIR own charge -> the Article that defines it.
        people = self.run(
            """
            MATCH (p:Person)-[r:INVOLVED_IN]->(k:Case) WHERE elementId(p) IN $ids
            OPTIONAL MATCH (:Crime {name: r.charge})<-[:DEFINES]-(a:Article)
            RETURN p.name AS person, p.aliases AS aliases, r.role AS role, r.charge AS charge,
                   r.sentence AS sentence, k.id AS case_id, k.name AS case_name, a.id AS article_id
            ORDER BY person, case_name
            """,
            ids=seed_ids,
        )
        for row in people:
            aliases = f" (bí danh: {', '.join(row['aliases'])})" if row["aliases"] else ""
            charge = f"tội {row['charge']} [{row['article_id']}]" if row["article_id"] else "chưa nêu tội danh"
            facts.append(f"{row['person']}{aliases} — {row['role'] or 'liên quan'} trong vụ '{row['case_name']}': "
                         f"{charge}; mức án: {row['sentence'] or 'chưa có'}")
            if row["article_id"]:
                law_pairs.append({"case_id": row["case_id"], "article_id": row["article_id"]})
        person_cases = {row["case_id"] for row in people if row["article_id"]}

        # 2. "Which cases involve <substance>?" -> every case in the graph, not only the retrieved ones.
        subs = [s for s in find_canonical_substances(question) if s != OTHER_SOLID]
        for row in self.run(
            """
            MATCH (k:Case)-[i:INVOLVES]->(s:Substance) WHERE s.name IN $subs
            RETURN s.name AS substance, k.name AS case_name, i.amount AS amount,
                   [(p:Person)-[r:INVOLVED_IN]->(k) WHERE r.role IN $roles | p.name] AS suspects
            ORDER BY substance, case_name
            """,
            subs=subs, roles=SUSPECT_ROLES,
        ):
            amount = f" ({row['amount']})" if row["amount"] else ""
            suspects = f"; đối tượng: {', '.join(row['suspects'][:8])}" if row["suspects"] else ""
            facts.append(f"Vụ việc có {row['substance']}{amount}: '{row['case_name']}'{suspects}")

        # 3. Cases reported by the retrieved articles (or touching a seed person): summary + case-level law.
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids
               OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids AND (s:NewsArticle OR s:Person) }
            RETURN k.id AS id, k.name AS name, k.summary AS summary,
                   COUNT { (k)<-[:REPORTS]-(:NewsArticle) } AS sources,
                   [(p:Person)-[r:INVOLVED_IN]->(k) |
                       p.name + CASE WHEN coalesce(r.sentence, '') <> '' THEN ': ' + r.sentence ELSE '' END] AS people,
                   [(k)-[i:INVOLVES]->(s:Substance) |
                       s.name + CASE WHEN coalesce(i.amount, '') <> '' THEN ' ' + i.amount ELSE '' END] AS substances,
                   [(k)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article) | a.id] AS articles
            ORDER BY name
            """,
            ids=seed_ids,
        )
        for c in cases:
            details = [f"{c['sources']} bài báo"]
            if c["people"]:
                details.append("người: " + ", ".join(c["people"][:12]))
            if c["substances"]:
                details.append("chất: " + ", ".join(c["substances"]))
            facts.append(f"Vụ việc '{c['name']}' ({'; '.join(details)}): {c['summary'] or ''}".rstrip(": "))
            if c["id"] not in person_cases:
                law_pairs += [{"case_id": c["id"], "article_id": a} for a in c["articles"]]

        # 4. Legal basis: khoản 1 (khung cơ bản) + the top-severity khoản + the khoản whose weight range
        #    contains the case's amount (MENTIONS {min_g, max_g} vs INVOLVES {amount_g}).
        facts += self._law_facts(law_pairs, question)
        return _dedupe(facts + seed_edges)[:max_facts]

    def _law_facts(self, pairs: list[dict], question: str) -> list[str]:
        notes: dict[tuple[str, int], dict] = {}

        def note(row: dict, text: str | None) -> None:
            key = (row["article_id"], row["number"])
            entry = notes.setdefault(key, {**row, "notes": []})
            if text and text not in entry["notes"]:
                entry["notes"].append(text)

        article_ids = _dedupe([p["article_id"] for p in pairs])
        article_numbers = re.findall(r"[Đđ]iều\s+(\d+)", question)
        article_ids += [f"Điều {n} BLHS" for n in article_numbers]
        for row in self.run(
            """
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE a.id IN $ids AND (cl.number = 1 OR cl.is_max_clause)
            RETURN a.id AS article_id, a.title AS title, cl.number AS number, cl.penalty AS penalty,
                   cl.is_max_clause AS is_max
            """,
            ids=article_ids,
        ):
            if row["number"] == 1:
                note(row, "khung cơ bản")
            if row["is_max"]:
                note(row, "khung cao nhất")
        for row in self.run(
            """
            UNWIND $pairs AS pr
            MATCH (k:Case {id: pr.case_id})-[i:INVOLVES]->(s:Substance) WHERE i.amount_g IS NOT NULL
            MATCH (s)-[:IS_A*0..1]->(:Substance)<-[m:MENTIONS]-(cl:Clause)<-[:HAS_CLAUSE]-(a:Article {id: pr.article_id})
            WHERE m.min_g IS NOT NULL AND i.amount_g >= m.min_g AND (m.max_g IS NULL OR i.amount_g < m.max_g)
            RETURN DISTINCT a.id AS article_id, a.title AS title, cl.number AS number, cl.penalty AS penalty,
                   s.name AS substance, i.amount AS amount, i.amount_g AS grams, m.point AS point,
                   m.min_g AS min_g, m.max_g AS max_g
            """,
            pairs=pairs,
        ):
            limit = f"từ {row['min_g']:g} g" + (f" đến dưới {row['max_g']:g} g" if row["max_g"] else " trở lên")
            note(row, f"áp dụng theo khối lượng: {row['substance']} {row['amount']} ≈ {row['grams']:g} g, "
                      f"điểm {row['point']}: {limit}")
        return [_clause_fact(r["article_id"], r["title"], r["number"], r["penalty"], r["notes"])
                for _, r in sorted(notes.items())]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    if ontology() == "hint":
        graph.suggested_constraints()
        articles = [parse_law_article(d) for d in law_docs]
        for a in articles:
            graph.add_law_article(a)
        crimes = [a["crime"] for a in articles if a["crime"]]
        for d in news_docs:
            for case in extract_news_cases(d, lambda p: llm_fn(p, json_mode=True), crimes):
                graph.add_news_case(case, d)
        return

    graph.own_constraints()
    articles = [parse_law_article_own(d) for d in law_docs]
    for a in articles:
        graph.add_law_article_own(a)
    graph.add_substance_groups()
    crimes = [a["crime"] for a in articles if a["crime"]]
    for d in news_docs:   # sorted by doc_id = publication order, so later reports merge into earlier cases
        graph.add_news_article_own(d, extract_news_cases_own(d, lambda p: llm_fn(p, json_mode=True), crimes))

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids: list[str] = []
        for c in chunks:
            did = c.get("metadata", {}).get("doc_id")
            if did and did not in doc_ids:
                doc_ids.append(did)
        facts_list = self.graph.context(question, doc_ids)
        facts_text = "\n".join(f"- {f}" for f in facts_list) if facts_list else "Không có dữ kiện bổ sung từ graph."
        chunks_text = "\n\n".join(f"[{i}] {chunk['content']}" for i, chunk in enumerate(chunks, start=1))
        prompt = GRAPH_PROMPT.format(
            facts=facts_text,
            chunks=chunks_text,
            question=question,
        )
        return self.llm_fn(prompt)
