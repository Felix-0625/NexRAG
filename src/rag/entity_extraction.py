"""Entity normalization and dependency-backed relation extraction."""

import hashlib
import re
import unicodedata
from functools import lru_cache
from threading import RLock

_LABELS = {"PERSON", "ORG", "GPE", "LOC", "FAC", "PRODUCT", "EVENT", "LAW", "WORK_OF_ART"}
_GENERIC = {
    "company",
    "agreement",
    "party",
    "seller",
    "buyer",
    "inc",
    "ltd",
    "公司",
    "甲方",
    "乙方",
}
_SUBJECTS = {"nsubj", "nsubjpass", "nsubj:pass", "csubj", "csubjpass"}
_OBJECTS = {"dobj", "obj", "iobj", "attr", "oprd", "pobj", "obl", "dative"}
_LOCK = RLock()


def normalize(text: str) -> str:
    """Normalize."""
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold()))


def text_hash(text: str) -> str:
    """Text hash."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=2)
def _model(language: str):
    import spacy

    return spacy.load("zh_core_web_sm" if language == "zh" else "en_core_web_sm")


def _language(text: str) -> str:
    return (
        "zh"
        if len(re.findall(r"[\u3400-\u9fff]", text)) * 3 > sum(char.isalpha() for char in text)
        else "en"
    )


def warm_up() -> None:
    """Warm up."""
    with _LOCK:
        _model("en")
        _model("zh")


def extractor_version() -> str:
    """Extractor version."""
    import spacy

    return f"spacy-{spacy.__version__}/en-zh-sm-3.8.0/svo-1"


def extract(text: str) -> dict:
    """Extract entity mentions and dependency-backed directed relations."""
    with _LOCK:
        document = _model(_language(text))(text)
    entities = []
    spans = {}
    for span in document.ents:
        name = normalize(span.text)
        if span.label_ not in _LABELS or len(name) < 2 or len(name) > 200 or name in _GENERIC:
            continue
        key = text_hash(span.label_ + "\0" + name)
        entities.append(
            {
                "key": key,
                "name": span.text,
                "normalized": name,
                "label": span.label_,
                "start": span.start_char,
                "end": span.end_char,
            }
        )
        for token in span:
            spans[token.i] = key
    edges = {}

    def in_subtree(token):
        """In subtree."""
        return {spans[word.i] for word in token.subtree if word.i in spans}

    for sentence in document.sents:
        if sentence.end_char - sentence.start_char > 4000:
            continue
        for predicate in sentence:
            subjects = [child for child in predicate.children if child.dep_ in _SUBJECTS]
            objects = [(child, "") for child in predicate.children if child.dep_ in _OBJECTS]
            for child in predicate.children:
                if child.dep_ == "prep":
                    objects.extend(
                        (item, "_" + child.lower_)
                        for item in child.children
                        if item.dep_ in _OBJECTS
                    )
            negated = any(
                child.dep_ == "neg" or child.lower_ in {"not", "never", "未", "不", "没有"}
                for child in predicate.children
            )
            for subject in subjects:
                for obj, preposition in objects:
                    relation = (
                        ("not:" if negated else "")
                        + (predicate.lemma_ or predicate.text)
                        + preposition
                    )
                    for source in in_subtree(subject):
                        for target in in_subtree(obj):
                            if source != target:
                                edges[source, target, relation, sentence.start_char] = {
                                    "source": source,
                                    "target": target,
                                    "predicate": relation,
                                    "start": sentence.start_char,
                                    "end": sentence.end_char,
                                    "quote": text[sentence.start_char : sentence.end_char],
                                }
            # An agreement's explicitly listed parties form a supported relation.
            if predicate.lower_ == "between" and any(
                token.lemma_.casefold() == "agreement" for token in sentence
            ):
                parties = sorted(in_subtree(predicate))
                if 2 <= len(parties) <= 4:
                    for index, source in enumerate(parties):
                        for target in parties[index + 1 :]:
                            edges[source, target, "agreement_between", sentence.start_char] = {
                                "source": source,
                                "target": target,
                                "predicate": "agreement_between",
                                "start": sentence.start_char,
                                "end": sentence.end_char,
                                "quote": text[sentence.start_char : sentence.end_char],
                            }
    return {"entities": entities, "relations": list(edges.values())}
