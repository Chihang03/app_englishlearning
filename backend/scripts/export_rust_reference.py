"""Freeze migration contracts and synthetic FSRS examples; never open a user database."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import random
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from importlib.metadata import version

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))


def route_inventory():
    records = []
    for path in sorted((BACKEND / "app").glob("*.py")):
        tree = ast.parse(path.read_text())
        prefix = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "APIRouter":
                prefix = next((k.value.value for k in node.value.keywords if k.arg == "prefix"), "")
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr in {"get", "post", "patch", "put", "delete"}):
                    continue
                route = prefix + decorator.args[0].value
                method = decorator.func.attr.upper()
                # Conservative mutation classification. GET /api/next allocates an attempt.
                writes = (method not in {"GET", "HEAD"} or route == "/api/next")
                if route in {"/api/words", "/api/words/import", "/api/admin/report-review/validate"}:
                    writes = False
                dependencies = []
                for default in [*node.args.defaults, *node.args.kw_defaults]:
                    if isinstance(default, ast.Call) and getattr(default.func,"id","") == "Depends":
                        dependencies.append(default.args[0].id)
                records.append({"method":method, "path":route, "source":str(path.relative_to(ROOT)),
                    "handler":node.name, "dependencies":dependencies, "writes":writes})
    return records


def fsrs_cases():
    if version("fsrs") != "6.3.0":
        raise RuntimeError("Reference requires fsrs==6.3.0")
    from fsrs import Card, Rating, State
    from app.adaptive_memory import scheduler
    rng = random.Random(20261022)
    now = datetime(2026,10,22,12,34,56,123456,tzinfo=timezone.utc)
    result = []
    for index in range(180):
        state = [State.Learning,State.Review,State.Relearning][index % 3]
        card = Card(card_id=index+1,state=state,step=0 if state != State.Review else None,due=now)
        if index >= 4:
            elapsed = [0,1,86399,86400,86401,30*86400,400*86400][index%7]
            card.last_review = now-timedelta(seconds=elapsed)
            card.stability = rng.uniform(0.001,365)
            card.difficulty = rng.uniform(1,10)
        elif state != State.Learning:
            card.stability,card.difficulty = 1.0,5.0
        model = scheduler([0.5,1.0,2.0][index%3])
        rating = Rating(index%4+1)
        updated,_ = model.review_card(card,rating,now)
        result.append({"card":card.to_dict(),"rating":int(rating),"now":now.isoformat(),
            "retention":model.desired_retention,"expected":updated.to_dict(),
            "recall":model.get_card_retrievability(card,now)})
    return result


def documents():
    # Imports do not enter ASGI lifespan; DATA_DIR is isolated even if that changes later.
    with tempfile.TemporaryDirectory(prefix="english-reference-") as directory:
        previous = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = directory
        try:
            from app.main import app
            from app.report_review import PROMPT, ReviewResult
            schema=app.openapi()
            return {"routes.json":route_inventory(),"openapi.json":schema,"fsrs-v6.3.0.json":fsrs_cases(),
                "field-order.json":{name:list(model.get('properties',{})) for name,model in schema['components']['schemas'].items()},
                "schema-v22.json":native_schema(), "sql.json":frozen_sql(), "report-review.json":{"prompt":PROMPT,"result_schema":ReviewResult.model_json_schema()}}
        finally:
            if previous is None: os.environ.pop("DATA_DIR",None)
            else: os.environ["DATA_DIR"] = previous


def native_schema():
    import sqlite3
    from contextlib import closing
    from app import migrations
    with tempfile.TemporaryDirectory(prefix="english-schema-") as temporary:
        path=Path(temporary)/"schema.db"
        migrations.run_migrations(path)
        with closing(sqlite3.connect(path)) as connection:
            return [row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name")]


def frozen_sql():
    from app import sense_learning,learning_filters,learning_units,database,senses,main,admin
    result={}
    modules=[(sense_learning,['learning_metrics','sense_card_content','next_sense_card','record_sense_review']),
        (database,['get_vocabulary_lists']),(senses,['senses_for_word']),(main,['stats']),(admin,['overview','users','user_detail','reports','report_detail_from'])]
    for module,names in modules:
        tree=ast.parse(Path(module.__file__).read_text())
        for node in tree.body:
            if not isinstance(node,ast.FunctionDef) or node.name not in names:continue
            environment={**vars(module),'visible':sense_learning.selected_filter(0)[0]}
            queries=[]
            for call in ast.walk(node):
                if isinstance(call,ast.Call) and isinstance(call.func,ast.Attribute) and call.func.attr=='execute' and call.args:
                    try:query=eval(compile(ast.Expression(call.args[0]),'<frozen-application-sql>','eval'),environment)
                    except (NameError,KeyError):continue
                    if isinstance(query,str):queries.append((call.lineno,query))
            for index,(_,query) in enumerate(sorted(queries)):result[f'{node.name}/{index}']=query
    result.update(due=sense_learning._DUE_SENSES_SQL,usable=learning_units.usable_unit_sql(),selected=sense_learning.selected_filter(0)[0],
        learnable_word=learning_filters.learnable_word_sql('__USER__'),learnable_unit=learning_filters.learnable_word_sql('__USER__',unit_expression='__UNIT__'),
        basic_unit=learning_filters.basic_word_sql(unit_expression='binding.learning_unit_id'))
    return result


def content_index(check):
    from app.learning_filters import basic_word_sql
    path = ROOT / "rust/crates/storage/src/api/content_index.sql"
    source = path.read_text()
    generated = source
    def lazy_basic(expression):
        # CASE is short-circuiting in SQLite. Boolean expressions in SELECT
        # need not be, so avoid grammatical subqueries for unrelated spellings.
        head, forms = expression[1:-1].split(" OR (", 1)
        condition, grammar = forms[:-1].split("\n        AND ", 1)
        return f"CASE WHEN {head} THEN 1 WHEN {condition} THEN ({grammar}) ELSE 0 END"
    for name, expression in [("WORD", lazy_basic(basic_word_sql())), ("UNIT", lazy_basic(basic_word_sql(unit_expression="u.id")))]:
        pattern = rf"(/\* BASIC_{name}_BEGIN \*/ ).*?( /\* BASIC_{name}_END \*/)"
        generated, count = re.subn(pattern, lambda m: m[1] + expression + m[2], generated, flags=re.S)
        if count != 1:
            raise RuntimeError("Missing content index generation marker: " + name)
    if check and generated != source:
        raise SystemExit(f"Reference drift: {path}")
    if not check:
        path.write_text(generated)
    print(f"{'checked' if check else 'wrote'} content_index.sql")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT / "rust" / "fixtures")
    parser.add_argument("--check",action="store_true")
    args = parser.parse_args()
    content_index(args.check)
    for name,value in documents().items():
        encoded = json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+"\n"
        path = args.output/name
        if args.check:
            if not path.exists() or path.read_text() != encoded:
                raise SystemExit(f"Reference drift: {path}")
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(encoded)
        print(f"{'checked' if args.check else 'wrote'} {name}")


if __name__ == "__main__":
    main()
