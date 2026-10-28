# ToolHub: implementation

A small, authenticated Flask app that hosts static HTML tools and Python (WSGI) tools from a single repo. The filesystem is the registry, so there is no database.

## Repo layout

```
toolhub/
├── app.py
├── requirements.txt
├── tools/        # one folder per tool (created at runtime)
│   └── <slug>/
│       ├── tool.json
│       └── index.html | app.py
└── data/         # per-tool JSON files for Python tools (created at runtime)
    └── <slug>/*.json
```

## Run

```bash
pip install -r requirements.txt
ADMIN_USER=me ADMIN_PASSWORD=something-strong SECRET_KEY=$(openssl rand -hex 32) python app.py
```

Open http://127.0.0.1:8000. Without the env vars it falls back to `admin/admin` and prints a warning.

## `requirements.txt`

```
flask>=3.0
```

## `app.py`

```python
"""ToolHub: authenticated host for static HTML tools and small Python (WSGI) tools.

Run:  ADMIN_USER=me ADMIN_PASSWORD=change-me SECRET_KEY=$(openssl rand -hex 32) python app.py
"""
import ast, importlib.util, json, os, re, secrets, shutil, sys, tempfile, zipfile
from pathlib import Path

from flask import (Flask, abort, flash, redirect, render_template_string,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.wrappers import Response

BASE = Path(__file__).parent.resolve()
TOOLS, DATA = BASE / "tools", BASE / "data"
TOOLS.mkdir(exist_ok=True); DATA.mkdir(exist_ok=True)

MAX_UNZIPPED = 50 * 1024 * 1024  # zip-bomb guard

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),  # set it, or sessions reset on restart
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
USER = os.environ.get("ADMIN_USER", "admin")
PW_HASH = generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin"))
if "ADMIN_PASSWORD" not in os.environ:
    print("WARNING: using default admin/admin. Set ADMIN_USER and ADMIN_PASSWORD.")


# ---------- auth: everything is protected except /login ----------
@app.before_request
def guard():
    if request.endpoint in ("login", "static"):
        return
    if not session.get("user"):
        return redirect(url_for("login", next=request.path))
    if request.method in ("POST", "PUT", "DELETE") and not request.path.startswith("/t/"):
        token = request.headers.get("X-CSRF") or request.form.get("csrf")
        if not token or token != session.get("csrf"):
            abort(400, "bad CSRF token")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("user") == USER and check_password_hash(PW_HASH, request.form.get("password", "")):
            session.clear()
            session.update(user=USER, csrf=secrets.token_hex(16))
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        flash("Invalid credentials")
    return render_template_string(PAGE, body=LOGIN)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- registry (filesystem is the source of truth) ----------
def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48] or "tool"


def list_tools():
    out = []
    for d in sorted(TOOLS.iterdir()):
        if (d / "tool.json").exists():
            out.append({"slug": d.name, **json.loads((d / "tool.json").read_text())})
    return out


def get_tool(slug):
    d = TOOLS / slug
    if not re.fullmatch(r"[a-z0-9-]+", slug) or not (d / "tool.json").exists():
        abort(404)
    return d, json.loads((d / "tool.json").read_text())


# ---------- install from .html, .py or .zip ----------
def has_wsgi_entry(path):
    """True if the file defines `create_app(...)` or a top-level `app = ...`."""
    for n in ast.parse(path.read_text()).body:
        if isinstance(n, ast.FunctionDef) and n.name == "create_app":
            return True
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "app" for t in n.targets):
            return True
    return False


def install(fs, name=""):
    fname = (fs.filename or "").lower()
    tmp = Path(tempfile.mkdtemp())
    try:
        if fname.endswith(".zip"):
            with zipfile.ZipFile(fs) as z:
                if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
                    raise ValueError("Zip too large when extracted")
                for m in z.namelist():  # zip-slip guard
                    if not (tmp / m).resolve().is_relative_to(tmp.resolve()):
                        raise ValueError("Unsafe path in zip")
                z.extractall(tmp)
            entries = [p for p in tmp.iterdir() if p.name != "__MACOSX"]
            src = entries[0] if len(entries) == 1 and entries[0].is_dir() else tmp
        elif fname.endswith((".html", ".htm")):
            src = tmp
            fs.save(tmp / "index.html")
        elif fname.endswith(".py"):
            src = tmp
            fs.save(tmp / "app.py")  # a single source file becomes the tool's app.py
        else:
            raise ValueError("Upload a .zip, .html or .py file")

        meta = json.loads((src / "tool.json").read_text()) if (src / "tool.json").exists() else {}
        if (src / "app.py").exists():
            kind = "python"
        elif (src / "index.html").exists():
            kind = "static"
        else:
            raise ValueError("Need an index.html (static) or app.py (python) at the root")

        if kind == "python" and not has_wsgi_entry(src / "app.py"):
            raise ValueError("app.py must define create_app(data_dir) or a top-level `app` (Flask/WSGI)")

        title = name or meta.get("name") or Path(fs.filename).stem.replace("_", " ")
        slug = slugify(meta.get("slug") or title)
        dest = TOOLS / slug
        if dest.exists():
            shutil.rmtree(dest)
        _cache.pop(slug, None)
        shutil.copytree(src, dest)
        (dest / "tool.json").write_text(json.dumps(
            {"name": title, "description": meta.get("description", ""), "type": kind}, indent=2))
        return slug
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- serving tools ----------
_cache = {}


def load_wsgi(slug, d):
    if slug not in _cache:
        spec = importlib.util.spec_from_file_location(f"tool_{slug}", d / "app.py")
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(d))
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path.remove(str(d))
        (DATA / slug).mkdir(exist_ok=True)
        # tool exposes either `create_app(data_dir)` or a ready `app`
        _cache[slug] = mod.create_app(str(DATA / slug)) if hasattr(mod, "create_app") else mod.app
    return _cache[slug]


@app.route("/t/<slug>/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/t/<slug>/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def serve_tool(slug, path):
    d, meta = get_tool(slug)
    if meta["type"] == "static":
        target = d / path
        if not path or target.is_dir():
            path = f"{path.rstrip('/')}/index.html".lstrip("/")
        if path == "tool.json":
            abort(404)
        return send_from_directory(d, path)
    env = request.environ.copy()
    env["SCRIPT_NAME"] = request.script_root + f"/t/{slug}"
    env["PATH_INFO"] = "/" + path
    return Response.from_app(load_wsgi(slug, d), env)


# ---------- dashboard ----------
@app.route("/")
def index():
    return render_template_string(PAGE, body=HOME, tools=list_tools())


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f:
        flash("Choose a file")
        return redirect("/")
    try:
        slug = install(f, request.form.get("name", "").strip())
        return redirect(f"/t/{slug}/")
    except (ValueError, SyntaxError, zipfile.BadZipFile, json.JSONDecodeError) as e:
        flash(f"Upload failed: {e}")
        return redirect("/")


@app.route("/delete/<slug>", methods=["POST"])
def delete(slug):
    d, _ = get_tool(slug)
    shutil.rmtree(d)
    _cache.pop(slug, None)  # tool data in data/<slug> is kept on purpose
    return redirect("/")


PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>ToolHub</title>
<style>
:root{--bg:#fafafa;--fg:#1a1a1a;--card:#fff;--bd:#ddd;--ac:#2563eb}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--card:#222;--bd:#333;--ac:#60a5fa}}
body{font:16px system-ui;background:var(--bg);color:var(--fg);max-width:900px;margin:2rem auto;padding:0 1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px}
.card a.t{color:var(--ac);font-weight:600;text-decoration:none}
input,button{font:inherit;padding:8px;border-radius:6px;border:1px solid var(--bd);background:var(--card);color:var(--fg)}
button{cursor:pointer}small{opacity:.7}.flash{color:#dc2626}
header{display:flex;justify-content:space-between;align-items:center}
</style>{{ body|safe }}"""

# bodies are rendered as nested templates
LOGIN = """<h2>ToolHub login</h2>
{% for m in get_flashed_messages() %}<p class=flash>{{m}}</p>{% endfor %}
<form method=post><p><input name=user placeholder=user autofocus></p>
<p><input name=password type=password placeholder=password></p><button>Sign in</button></form>"""

HOME = """<header><h2>ToolHub</h2>
<form action=/logout method=post><input type=hidden name=csrf value="{{session.csrf}}"><button>Logout</button></form></header>
{% for m in get_flashed_messages() %}<p class=flash>{{m}}</p>{% endfor %}
<div class=card style="margin-bottom:16px"><b>Add a tool</b> <small>(.html, .py or .zip with index.html / app.py)</small>
<form action=/upload method=post enctype=multipart/form-data style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
<input type=hidden name=csrf value="{{session.csrf}}"><input type=file name=file required>
<input name=name placeholder="Name (optional)"><button>Upload</button></form></div>
<div class=grid>{% for t in tools %}<div class=card>
<a class=t href="/t/{{t.slug}}/">{{t.name}}</a> <small>{{t.type}}</small>
<p><small>{{t.description}}</small></p>
<form action="/delete/{{t.slug}}" method=post onsubmit="return confirm('Delete {{t.name}}?')">
<input type=hidden name=csrf value="{{session.csrf}}"><button>Delete</button></form></div>
{% else %}<p>No tools yet. Upload one above.</p>{% endfor %}</div>"""


# render the inner bodies as templates too
_orig = render_template_string
def render_template_string(tpl, body=None, **ctx):  # noqa: F811
    if body is not None:
        ctx["body"] = _orig(body, session=session, **ctx)
    return _orig(tpl, **ctx)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
```

## Adding tools

Upload a single `.html` file, a single `.py` file, or a `.zip` from the dashboard. You can also drop a folder with a `tool.json` into `tools/` and commit it.

| Type | Required at zip root (or in one top-level folder) | Served at |
|------|---------------------------------------------------|-----------|
| Static | `index.html` plus any assets (or a single `.html` upload) | `/t/<slug>/` |
| Python | `app.py` exposing `create_app(data_dir)` or a WSGI `app` (or a single `.py` upload, saved as `app.py`) | `/t/<slug>/` |

A Python tool must have a web interface. On upload, the source is checked for `create_app(...)` or a top-level `app = ...`. A plain script that only prints output is rejected with a clear error message, and an invalid file is rejected too.

Optional `tool.json` in a zip:

```json
{ "name": "My Tool", "description": "What it does", "slug": "my-tool" }
```

### Example Python tool (upload this as a single `.py` file, or inside a zip)

```python
# app.py
import json, os
from flask import Flask, jsonify, request

def create_app(data_dir):
    app = Flask(__name__)
    path = os.path.join(data_dir, "notes.json")

    @app.get("/")
    def index():
        return "Notes tool"

    @app.get("/notes")
    def notes():
        return jsonify(json.load(open(path)) if os.path.exists(path) else [])

    @app.post("/notes")
    def add():
        items = json.load(open(path)) if os.path.exists(path) else []
        items.append(request.get_json())
        json.dump(items, open(path, "w"))
        return jsonify(ok=True)

    return app
```

### Data

There is no data API. Tools read and write their own JSON files directly.

- **Python tools** get a private folder, `data/<slug>/`, passed to `create_app(data_dir)`. They open files there with plain `json.load` / `json.dump`, as in the example above. This folder is kept when the tool is updated, re-uploaded or deleted.
- **Updating tools**: Each tool card in the Admin dashboard has an Update tool section to upload an updated version directly to that tool slug without risk of renaming or losing data. Any data files (.json, .sqlite, etc.) mistakenly created inside tools/<slug>/ are also safely migrated to data/<slug>/ before code replacement.
- **Single-file `.py` tools** that use a top-level `app` instead of `create_app` can pick their own path, for example `Path(__file__).parent / "data.json"`. Note that this lives inside the tool folder and is replaced on re-upload.
- **Static HTML tools** run in the browser, so they cannot write files. They can read JSON files bundled in the zip with `fetch('data.json')`. A tool that needs to save anything should be a Python tool.

## Limitations

- Python tools run in the same process as the hub, so only allow trusted users to upload.
- Single user from env vars. Multiple users or roles would need a `users.json` with an `admin` flag gating upload and delete.
- A tool's own `requirements.txt` is not installed automatically.
- Static HTML tools are read-only (they can load bundled JSON but not save). Use a Python tool when you need to write data.
- Tool routes under `/t/` are behind login but skip the hub's CSRF check, because tools define their own forms. SameSite=Lax cookies limit cross-site posts.
- For production, run behind gunicorn with HTTPS, set `SESSION_COOKIE_SECURE=True`, and keep `SECRET_KEY` stable.
