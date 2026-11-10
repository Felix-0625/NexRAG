"""Require direct predicate literals to be grounded in the current request before execution.

This extra correctness check is neither SQL authorization nor general semantic equivalence;
computed expressions still need review. Source views and read-only guards remain authoritative.
"""

import re
import unicodedata
from decimal import Decimal, InvalidOperation

import sqlglot
from sqlglot import exp


def require_filter_basis(value, prompt: str, *, tenant_id: str = "") -> None:
    """Require a filter literal in the current question, except the server-authorized tenant ID."""
    text = unicodedata.normalize("NFKC", prompt).casefold()
    literal = unicodedata.normalize("NFKC", str(value)).casefold()
    if literal == tenant_id.casefold() and tenant_id:
        return  # The server already authorized this identity; it cannot select another scope.
    try:
        number = Decimal(literal)
    except InvalidOperation:
        literal = literal.strip("%_")  # LIKE wildcards are syntax, not business values.
        pattern = re.escape(literal)
        if literal.isascii():
            pattern = rf"(?<![a-z0-9_]){pattern}(?![a-z0-9_])"
        supported = bool(literal and re.search(pattern, text))
    else:
        supported = number in {
            Decimal(raw) for raw in re.findall(r"(?<![0-9.])-?\d+(?:\.\d+)?(?![0-9.])", text)
        }
    if not supported:
        raise ValueError(
            "筛选值没有当前问题中的直接依据，未执行查询。"
            "不要添加用户未要求的状态、日期或其他筛选；如确有歧义，请先明确具体条件。"
        )


def require_query_filter_basis(sql: str, prompt: str, *, tenant_id: str = "") -> None:
    """Check direct WHERE and HAVING literals without treating this as SQL authorization."""
    tree = sqlglot.parse_one(sql, read="sqlite")
    comparisons = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)
    for clause in tree.find_all(exp.Where, exp.Having):
        for predicate in clause.walk():
            values = []
            if isinstance(predicate, comparisons) and predicate.find(exp.Column):
                values = [predicate.this, predicate.expression]
            elif isinstance(predicate, exp.In) and isinstance(predicate.this, exp.Column):
                values = list(predicate.expressions)
            elif isinstance(predicate, exp.Between) and isinstance(predicate.this, exp.Column):
                values = [predicate.args["low"], predicate.args["high"]]
            for value in values:
                if isinstance(value, exp.Literal):
                    require_filter_basis(value.this, prompt, tenant_id=tenant_id)
                elif isinstance(value, exp.Neg) and isinstance(value.this, exp.Literal):
                    require_filter_basis("-" + value.this.this, prompt, tenant_id=tenant_id)
