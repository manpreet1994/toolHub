"""ToolHub: authenticated host for static HTML tools and small Python (WSGI) tools.

Run:  ADMIN_USER=me ADMIN_PASSWORD=change-me SECRET_KEY=$(openssl rand -hex 32) python app.py
"""
import ast, importlib.util, json, os, re, secrets, shutil, sys, tempfile, traceback, zipfile
from pathlib import Path

from dotenv import load_dotenv
from flask import (Flask, abort, flash, redirect, render_template_string,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.wrappers import Response

BASE = Path(__file__).parent.resolve()
load_dotenv(BASE / ".env")
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
            nxt = request.args.get("next", "/admin")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/admin")
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


def install(fs, name="", target_slug=None):
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

        if target_slug:
            slug = target_slug
            dest = TOOLS / slug
            old_meta = json.loads((dest / "tool.json").read_text()) if (dest / "tool.json").exists() else {}
            title = name or old_meta.get("name") or meta.get("name") or slug.replace("-", " ")
            description = meta.get("description") or old_meta.get("description", "")
        else:
            title = name or meta.get("name") or Path(fs.filename).stem.replace("_", " ")
            slug = slugify(meta.get("slug") or title)
            description = meta.get("description", "")
            dest = TOOLS / slug

        (DATA / slug).mkdir(exist_ok=True)
        if dest.exists():
            # Safety net: preserve any data files mistakenly stored inside tools/<slug>
            for item in dest.iterdir():
                if item.is_file() and item.name != "tool.json" and item.suffix in (".json", ".sqlite", ".sqlite3", ".db", ".csv"):
                    dest_data_file = DATA / slug / item.name
                    if not dest_data_file.exists():
                        shutil.copy2(item, dest_data_file)
            shutil.rmtree(dest)

        _cache.pop(slug, None)
        shutil.copytree(src, dest)
        (dest / "tool.json").write_text(json.dumps(
            {"name": title, "description": description, "type": kind}, indent=2))
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
    try:
        return Response.from_app(load_wsgi(slug, d), env)
    except Exception:
        tb = traceback.format_exc()
        app.logger.error(f"Error serving tool '{slug}':\n{tb}")
        return render_template_string(
            PAGE,
            body=f"<div class=card style='border-color:#dc2626;background:rgba(220,38,38,0.05)'>"
                 f"<h3 style='color:#dc2626;margin-top:0'>Error running tool: {meta.get('name', slug)}</h3>"
                 f"<p><small>The tool encountered an unhandled exception:</small></p>"
                 f"<pre style='overflow-x:auto;padding:10px;background:var(--card);border-radius:6px;font-size:13px;white-space:pre-wrap'>{tb}</pre>"
                 f"<p><a class=t href='/admin'>&larr; Return to Admin</a></p></div>"
        ), 500


# ---------- dashboard ----------
@app.route("/")
def index():
    return redirect(url_for("tools"))


@app.route("/tools")
def tools():
    return render_template_string(PAGE, body=TOOLS_PAGE, tools=list_tools())


@app.route("/admin")
def admin():
    return render_template_string(PAGE, body=ADMIN_HOME, tools=list_tools())


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f:
        flash("Choose a file")
        return redirect(url_for("admin"))
    try:
        slug = install(f, request.form.get("name", "").strip())
        return redirect(url_for("admin"))
    except (ValueError, SyntaxError, zipfile.BadZipFile, json.JSONDecodeError) as e:
        flash(f"Upload failed: {e}")
        return redirect(url_for("admin"))


@app.route("/update/<slug>", methods=["POST"])
def update(slug):
    d, meta = get_tool(slug)
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a file to update")
        return redirect(url_for("admin"))
    try:
        install(f, target_slug=slug)
        flash(f"Updated '{meta.get('name', slug)}' successfully")
        return redirect(url_for("admin"))
    except (ValueError, SyntaxError, zipfile.BadZipFile, json.JSONDecodeError) as e:
        flash(f"Update failed: {e}")
        return redirect(url_for("admin"))


@app.route("/delete/<slug>", methods=["POST"])
def delete(slug):
    d, _ = get_tool(slug)
    shutil.rmtree(d)
    _cache.pop(slug, None)  # tool data in data/<slug> is kept on purpose
    return redirect(url_for("admin"))


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

ADMIN_HOME = """<header><h2>ToolHub admin</h2>
<nav style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
  <a href="/tools">Consumer view</a>
  <form action=/logout method=post><input type=hidden name=csrf value="{{session.csrf}}"><button>Logout</button></form>
</nav></header>
{% for m in get_flashed_messages() %}<p class=flash>{{m}}</p>{% endfor %}
<div class=card style="margin-bottom:16px"><b>Add a tool</b> <small>(.html, .py or .zip with index.html / app.py)</small>
<form action=/upload method=post enctype=multipart/form-data style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
<input type=hidden name=csrf value="{{session.csrf}}"><input type=file name=file required>
<input name=name placeholder="Name (optional)"><button>Upload</button></form></div>
<div class=grid>{% for t in tools %}<div class=card>
<strong>{{t.name}}</strong> <small>{{t.type}}</small>
<p><small>{{t.description or 'No description provided.'}}</small></p>
<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
  <a class=t href="/t/{{t.slug}}/">Open</a>
  <form action="/delete/{{t.slug}}" method=post onsubmit="return confirm('Delete {{t.name}}?')">
  <input type=hidden name=csrf value="{{session.csrf}}"><button>Delete</button></form>
</div>
<details style="margin-top:10px">
  <summary style="cursor:pointer;color:var(--ac);font-size:13px;font-weight:600">Update tool</summary>
  <form action="/update/{{t.slug}}" method=post enctype="multipart/form-data" style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">
    <input type=hidden name=csrf value="{{session.csrf}}">
    <input type=file name=file required style="font-size:12px;padding:4px">
    <button style="padding:4px 10px;font-size:13px">Upload & Update</button>
  </form>
</details></div>
{% else %}<p>No tools yet. Upload one above.</p>{% endfor %}</div>"""

TOOLS_PAGE = """<header><h2>Tool catalog</h2>
<nav style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
  <a href="/admin">Admin</a>
  <form action=/logout method=post><input type=hidden name=csrf value="{{session.csrf}}"><button>Logout</button></form>
</nav></header>
{% for m in get_flashed_messages() %}<p class=flash>{{m}}</p>{% endfor %}
<p><small>Only placeholders are listed here. Each tool loads only when opened.</small></p>
<div class=grid>{% for t in tools %}<div class=card>
<strong>{{t.name}}</strong> <small>{{t.type}}</small>
<p><small>{{t.description or 'No description provided.'}}</small></p>
<a class=t href="/t/{{t.slug}}/">Open tool</a>
</div>{% else %}<p>No tools are available yet.</p>{% endfor %}</div>"""


# render the inner bodies as templates too
_orig = render_template_string
def render_template_string(tpl, body=None, **ctx):  # noqa: F811
    if body is not None:
        ctx["body"] = _orig(body, session=session, **ctx)
    return _orig(tpl, **ctx)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
