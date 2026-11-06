import hmac
import json
import os
import re
import secrets
import time
import uuid
import zipfile
from difflib import get_close_matches
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
from urllib.parse import urlparse

from authlib.integrations.flask_client import OAuth
from flask import (Flask, abort, flash, g, jsonify, make_response, redirect, render_template,
                   request, send_file, send_from_directory, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, or_, inspect, text
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix

import providers

DATA = os.environ.get("DATA_DIR", "/data")
UPLOADS = os.environ.get("UPLOAD_DIR", DATA + "/uploads")
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "3600"))
DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", "EUR")
ALLOWED = {"png": "image", "jpg": "image", "jpeg": "image", "webp": "image", "gif": "image", "pdf": "pdf"}
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "")
os.makedirs(UPLOADS, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "change-me"),
    SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///" + DATA + "/app.db"),
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1048576,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PUBLIC_URL", "").startswith("https"),
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
db = SQLAlchemy(app)
oauth = OAuth(app)
if OIDC_ISSUER:
    oauth.register(
        "authentik",
        server_metadata_url=OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration",
        client_id=os.environ.get("OIDC_CLIENT_ID"),
        client_secret=os.environ.get("OIDC_CLIENT_SECRET"),
        client_kwargs={"scope": "openid profile email"},
    )
cache = {}


@event.listens_for(Engine, "connect")
def enable_foreign_keys(connection, record):
    connection.cursor().execute("PRAGMA foreign_keys=ON")


def new_id():
    return uuid.uuid4().hex[:12]


def id_column():
    return db.Column(db.String(12), primary_key=True, default=new_id)


def fk(table, on_delete="CASCADE", **kwargs):
    return db.Column(db.String(12), db.ForeignKey(table + ".id", ondelete=on_delete), **kwargs)


def normalize(text):
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


class User(db.Model):
    id = id_column()
    sub = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255))
    email = db.Column(db.String(255))
    is_admin = db.Column(db.Boolean, default=False)


class Category(db.Model):
    id = id_column()
    name = db.Column(db.String(120), nullable=False)
    parent_id = fk("category", "SET NULL")
    parent = db.relationship("Category", remote_side=[id])

    @property
    def path(self):
        return (self.parent.path + " / " if self.parent else "") + self.name


class Part(db.Model):
    id = id_column()
    name = db.Column(db.String(255), nullable=False, index=True)
    manufacturer = db.Column(db.String(255), index=True)
    mpn = db.Column(db.String(255), index=True)
    description = db.Column(db.Text)
    category_id = fk("category", "SET NULL", index=True)
    tags = db.Column(db.String(500), default="")
    image_url = db.Column(db.String(1000))
    specs_json = db.Column(db.Text, default="[]")
    links_json = db.Column(db.Text, default="[]")
    ext_files_json = db.Column(db.Text, default="[]")
    sources_json = db.Column(db.Text, default="[]")
    created = db.Column(db.DateTime, default=datetime.utcnow)
    updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    category = db.relationship("Category")
    files = db.relationship("File", cascade="all, delete-orphan", backref="part", order_by="File.created")

    def specs(self):
        return json.loads(self.specs_json or "[]")

    def links(self):
        return json.loads(self.links_json or "[]")

    def ext_files(self):
        return json.loads(self.ext_files_json or "[]")

    def sources(self):
        return json.loads(self.sources_json or "[]")

    def tag_list(self):
        return [t.strip() for t in (self.tags or "").split(",") if t.strip()]

    def images(self):
        return [f for f in self.files if f.kind == "image"]

    def pdfs(self):
        return [f for f in self.files if f.kind == "pdf"]

    def has_datasheet(self):
        return bool(self.pdfs() or self.ext_files())

    def key(self):
        return normalize(self.manufacturer) + "|" + normalize(self.mpn or self.name)

    def related_query(self):
        return re.split(r"[-_/\s]", self.mpn or self.name)[0]

    def chain(self):
        chain, current = [], self.category
        while current:
            chain.insert(0, current)
            current = current.parent
        return chain

    def main_image(self):
        images = self.images()
        return url_for("file", fid=images[0].id) if images else self.image_url

    def best_prices(self, qty=1):
        best = {}
        for source in self.sources():
            for offer in source.get("offers", []):
                breaks = sorted(offer.get("breaks", []), key=lambda b: b["qty"] or 0)
                if not breaks:
                    continue
                usable = [b for b in breaks if (b["qty"] or 0) <= max(qty, breaks[0]["qty"] or 0)]
                choice = usable[-1]
                if choice["currency"] not in best or choice["price"] < best[choice["currency"]][0]:
                    best[choice["currency"]] = (choice["price"], offer["distributor"])
        return best

    def price_label(self):
        best = self.best_prices(1)
        if not best:
            return None
        currency = DEFAULT_CURRENCY if DEFAULT_CURRENCY in best else next(iter(best))
        price = best[currency][0]
        return (f"{price:.2f}" if price >= 1 else f"{price:.4f}") + " " + currency

    def last_fetched(self):
        fetched = [s["fetched"] for s in self.sources() if s.get("fetched")]
        return max(fetched) if fetched else None


class File(db.Model):
    id = id_column()
    part_id = fk("part", index=True)
    name = db.Column(db.String(255))
    stored = db.Column(db.String(100), unique=True)
    kind = db.Column(db.String(10))
    size = db.Column(db.Integer, default=0)
    created = db.Column(db.DateTime, default=datetime.utcnow)


class Note(db.Model):
    id = id_column()
    part_id = fk("part")
    user_id = fk("user")
    text = db.Column(db.Text)
    __table_args__ = (db.UniqueConstraint("part_id", "user_id"),)


class Project(db.Model):
    id = id_column()
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    owner_id = fk("user", "SET NULL", index=True)
    visibility = db.Column(db.String(20), default="private", nullable=False)
    share_token = db.Column(db.String(32), unique=True, index=True)
    created = db.Column(db.DateTime, default=datetime.utcnow)
    items = db.relationship("Item", cascade="all, delete-orphan", backref="project")
    owner = db.relationship("User")

    def cost(self):
        totals, missing = {}, 0
        for item in self.items:
            prices = item.part.best_prices(item.qty)
            missing += not prices
            for currency, (price, _) in prices.items():
                totals[currency] = totals.get(currency, 0) + price * item.qty
        return totals, missing


class Item(db.Model):
    id = id_column()
    project_id = fk("project")
    part_id = fk("part")
    qty = db.Column(db.Integer, default=1)
    notes = db.Column(db.String(500))
    part = db.relationship("Part")


class ProjectShare(db.Model):
    id = id_column()
    project_id = fk("project", index=True)
    user_id = fk("user", index=True)
    role = db.Column(db.String(10), default="read", nullable=False)
    __table_args__ = (db.UniqueConstraint("project_id", "user_id"),)


class Library(db.Model):
    id = id_column()
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    owner_id = fk("user", "SET NULL", index=True)
    visibility = db.Column(db.String(20), default="private", nullable=False)
    share_token = db.Column(db.String(32), unique=True, index=True)
    created = db.Column(db.DateTime, default=datetime.utcnow)
    owner = db.relationship("User")
    items = db.relationship("LibraryPart", cascade="all, delete-orphan", backref="library")


class LibraryPart(db.Model):
    id = id_column()
    library_id = fk("library")
    part_id = fk("part")
    category_id = fk("category", "SET NULL")
    note = db.Column(db.Text)
    part = db.relationship("Part")
    category = db.relationship("Category")
    __table_args__ = (db.UniqueConstraint("library_id", "part_id"),)


class LibraryShare(db.Model):
    id = id_column()
    library_id = fk("library", index=True)
    user_id = fk("user", index=True)
    role = db.Column(db.String(10), default="read", nullable=False)
    __table_args__ = (db.UniqueConstraint("library_id", "user_id"),)


class Setting(db.Model):
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text)


def provider_config(name):
    stored = {s.key: s.value for s in Setting.query.filter(Setting.key.like(name + ".%"))}
    config = {"enabled": stored.get(name + ".enabled") == "1"}
    for field in providers.PROVIDERS[name]["fields"]:
        config[field] = stored.get(name + "." + field) or os.environ.get(f"{name.upper()}_{field.upper()}", "")
    return config


def enabled_providers():
    return [n for n in providers.PROVIDERS if provider_config(n)["enabled"]]


def save_setting(key, value):
    row = db.session.get(Setting, key) or Setting(key=key)
    row.value = value
    db.session.add(row)


SEED = {
    "Semiconductors": ["Transistors", "MOSFETs", "Diodes", "Integrated Circuits"],
    "Passive Components": ["Resistors", "Capacitors", "Inductors"],
    "Development Boards": ["Arduino", "ESP32", "Raspberry Pi"],
    "Sensors": [], "Displays": [], "Connectors": [], "Power Management": [], "Modules": [], "Miscellaneous": [],
}


def init_db():
    db.create_all()
    # create_all does not add columns to installations created by older versions.
    columns = {c["name"] for c in inspect(db.engine).get_columns("project")}
    with db.engine.begin() as connection:
        if "owner_id" not in columns:
            connection.execute(text("ALTER TABLE project ADD COLUMN owner_id VARCHAR(12) REFERENCES user(id)"))
        if "visibility" not in columns:
            connection.execute(text("ALTER TABLE project ADD COLUMN visibility VARCHAR(20) NOT NULL DEFAULT 'private'"))
        if "share_token" not in columns:
            connection.execute(text("ALTER TABLE project ADD COLUMN share_token VARCHAR(32)"))
    if not Category.query.first():
        for name, children in SEED.items():
            parent = Category(name=name)
            db.session.add(parent)
            db.session.flush()
            db.session.add_all(Category(name=child, parent_id=parent.id) for child in children)
        db.session.commit()


with app.app_context():
    init_db()


def category_tree():
    counts = dict(db.session.query(Part.category_id, db.func.count(Part.id)).group_by(Part.category_id).all())
    kids = {}
    for category in Category.query.all():
        kids.setdefault(category.parent_id, []).append(category)

    def build(parent_id):
        nodes = []
        for category in sorted(kids.get(parent_id, []), key=lambda c: c.name.lower()):
            children = build(category.id)
            own = counts.get(category.id, 0)
            nodes.append({"id": category.id, "name": category.name, "own": own,
                          "count": own + sum(n["count"] for n in children), "children": children})
        return nodes
    return build(None)


def descendant_ids(category_id):
    found, queue = [category_id], [category_id]
    while queue:
        children = [c.id for c in Category.query.filter(Category.parent_id.in_(queue))]
        queue = [c for c in children if c not in found]
        found += queue
    return found


def resolve_category(form):
    path = form.get("category_new", "").strip()
    if not path:
        return form.get("category_id") or None
    parent_id = None
    for name in [n.strip() for n in path.split("/") if n.strip()]:
        found = Category.query.filter(db.func.lower(Category.name) == name.lower(),
                                      Category.parent_id == parent_id).first()
        if not found:
            found = Category(name=name, parent_id=parent_id)
            db.session.add(found)
            db.session.flush()
        parent_id = found.id
    return parent_id


def safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else None


@app.template_global()
def qs(**overrides):
    args = request.args.to_dict()
    args.update(overrides)
    return url_for("parts", **{k: v for k, v in args.items() if v not in (None, "")})


@app.template_filter("domain")
def domain(url):
    return urlparse(url).netloc.removeprefix("www.")


@app.template_filter("filesize")
def filesize(size):
    return f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{max(1, round((size or 0) / 1024))} KB"


def owned_projects():
    if not g.get("user"):
        return []
    legacy = Project.owner_id.is_(None) if g.user.is_admin else Project.owner_id == "__none__"
    return Project.query.filter(or_(Project.owner_id == g.user.id, legacy, Project.visibility == "public",
                                    Project.id.in_(db.session.query(ProjectShare.project_id).filter_by(user_id=g.user.id)))).order_by(Project.name).all()


def project_access(project, write=False):
    if not g.user:
        return False
    if project.owner_id == g.user.id or (project.owner_id is None and g.user.is_admin):
        return True
    share = ProjectShare.query.filter_by(project_id=project.id, user_id=g.user.id).first()
    if share and (not write or share.role == "write"):
        return True
    return project.visibility == "public" and not write


def library_access(library, write=False):
    if not g.user:
        return False
    if library.owner_id == g.user.id:
        return True
    share = LibraryShare.query.filter_by(library_id=library.id, user_id=g.user.id).first()
    if share and (not write or share.role == "write"):
        return True
    return library.visibility == "public" and not write


def accessible_libraries():
    if not g.get("user"):
        return []
    return Library.query.filter(or_(Library.owner_id == g.user.id, Library.visibility == "public",
                                    Library.id.in_(db.session.query(LibraryShare.library_id).filter_by(user_id=g.user.id)))).order_by(Library.name).all()


@app.before_request
def guard():
    session.setdefault("csrf", secrets.token_hex(16))
    if request.method == "POST":
        sent = request.form.get("csrf") or request.headers.get("X-CSRF", "")
        if not hmac.compare_digest(sent, session["csrf"]):
            abort(400)
    if request.path.startswith("/api/v1/"):
        return None
    g.user = db.session.get(User, session["uid"]) if "uid" in session else None
    if g.user is None and request.endpoint not in ("health", "login", "callback", "static", "public_project", "public_library"):
        session.pop("uid", None)
        return redirect(url_for("login"))


@app.context_processor
def inject():
    return {"csrf": session.get("csrf"), "user": g.get("user"), "projects_all": owned_projects(),
            "libraries_all": accessible_libraries(),
            "categories_all": Category.query.order_by(Category.name).all()}


def admin_only():
    if not g.user or not g.user.is_admin:
        abort(403)


def sign_in(sub, name, email):
    user = User.query.filter_by(sub=sub).first()
    if not user:
        user = User(sub=sub, is_admin=User.query.count() == 0)
        db.session.add(user)
    user.name, user.email = name, email
    db.session.commit()
    session.clear()
    session["uid"] = user.id
    session["csrf"] = secrets.token_hex(16)
    return redirect(url_for("parts"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if os.environ.get("DEV_AUTOLOGIN") == "1":
        return sign_in("dev", "Developer", "dev@localhost")
    if OIDC_ISSUER:
        return oauth.authentik.authorize_redirect(os.environ["OIDC_REDIRECT_URI"])
    if request.method == "POST":
        password = os.environ.get("LOCAL_PASSWORD", "")
        username = os.environ.get("LOCAL_USER", "admin")
        if password and hmac.compare_digest(request.form.get("password", ""), password) \
                and request.form.get("username") == username:
            return sign_in("local:" + username, username, "")
        flash("Invalid credentials or local login not configured.", "danger")
    return render_template("login.html")


@app.route("/callback")
def callback():
    info = oauth.authentik.authorize_access_token().get("userinfo") or {}
    return sign_in(info["sub"], info.get("name") or info.get("preferred_username"), info.get("email"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/health")
def health():
    return "ok"


@app.get("/")
def home():
    return redirect(url_for("parts"))


def local_query(q="", category_id=None, manufacturer=None, tag=None):
    query = Part.query
    for word in q.split():
        like = f"%{word}%"
        query = query.filter(or_(Part.name.ilike(like), Part.mpn.ilike(like), Part.manufacturer.ilike(like),
                                 Part.description.ilike(like), Part.tags.ilike(like)))
    if category_id:
        query = query.filter(Part.category_id.in_(descendant_ids(category_id)))
    if manufacturer:
        query = query.filter(db.func.lower(Part.manufacturer) == manufacturer.lower())
    if tag:
        query = query.filter(Part.tags.ilike(f"%{tag}%"))
    return query


@app.get("/parts")
def parts():
    args = request.args
    q = args.get("q", "").strip()
    category_id = args.get("cat")
    query = local_query(q, category_id, args.get("mf"), args.get("tag"))
    query = query.order_by(Part.name) if args.get("sort") == "name" else query.order_by(Part.updated.desc())
    category = db.session.get(Category, category_id) if category_id else None
    if category_id and category is None:
        category_id = None
        query = local_query(q, None, args.get("mf"), args.get("tag"))
    filtered = any(args.get(k) for k in ("cat", "mf", "tag"))
    return render_template(
        "parts.html", pagination=query.paginate(page=args.get("page", 1, type=int), per_page=24, error_out=False),
        tree=category_tree(), category=category, q=q, view=args.get("view", "cards"),
        open_ids=[c.id for c in category.chain()] if category else [], project_id=args.get("project"),
        names=enabled_providers() if q and not filtered else [],
        recent_projects=Project.query.order_by(Project.created.desc()).limit(5).all())


def parse_rows(names, values, units):
    return [{"name": n.strip(), "value": v.strip(), "unit": u.strip()} for n, v, u in zip(names, values, units) if n.strip()]


def store_uploads(part, field="files"):
    for upload in request.files.getlist(field):
        if not upload.filename:
            continue
        extension = upload.filename.rsplit(".", 1)[-1].lower()
        kind = ALLOWED.get(extension)
        if kind == "pdf" and upload.stream.read(5) != b"%PDF-":
            kind = None
        upload.stream.seek(0)
        if not kind:
            flash(f"Skipped {upload.filename}: unsupported file type.", "warning")
            continue
        stored = uuid.uuid4().hex + "." + extension
        path = os.path.join(UPLOADS, stored)
        upload.save(path)
        part.files.append(File(name=os.path.basename(upload.filename), stored=stored, kind=kind, size=os.path.getsize(path)))


def apply_details(part):
    form = request.form
    for field in ("name", "manufacturer", "mpn", "description", "image_url"):
        if field in form:
            setattr(part, field, form[field].strip() or None)
    if "tags" in form:
        part.tags = ",".join(t.strip() for t in form["tags"].split(",") if t.strip())
    if "category_id" in form or "category_new" in form:
        part.category_id = resolve_category(form)
    db.session.add(part)
    db.session.flush()
    store_uploads(part)
    db.session.commit()


@app.post("/parts/new")
def part_new():
    if not request.form.get("name", "").strip():
        flash("Name is required.", "danger")
        return redirect(request.referrer or url_for("parts"))
    part = Part()
    apply_details(part)
    flash("Part created.", "success")
    return redirect(url_for("part", pid=part.id))


def part_page(pid, tab=""):
    return redirect(url_for("part", pid=pid) + ("#" + tab if tab else ""))


@app.get("/parts/<pid>")
def part(pid):
    p = db.get_or_404(Part, pid)
    note = Note.query.filter_by(part_id=p.id, user_id=g.user.id).first()
    similar = local_query(category_id=p.category_id).filter(Part.id != p.id).limit(4).all() if p.category_id else []
    return render_template("part.html", p=p, note=note.text if note else "", similar=similar, names=enabled_providers())


@app.post("/parts/<pid>/details")
def part_details(pid):
    p = db.get_or_404(Part, pid)
    if not request.form.get("name", "").strip():
        flash("Name is required.", "danger")
    else:
        apply_details(p)
    return part_page(pid)


@app.post("/parts/<pid>/specs")
def part_specs(pid):
    p = db.get_or_404(Part, pid)
    f = request.form
    p.specs_json = json.dumps(parse_rows(f.getlist("name"), f.getlist("value"), f.getlist("unit")))
    db.session.commit()
    flash("Specifications saved.", "success")
    return part_page(pid, "specs")


@app.post("/parts/<pid>/links")
def part_links(pid):
    p = db.get_or_404(Part, pid)
    links = p.links()
    if request.form["action"] == "delete":
        index = request.form.get("index", type=int)
        if index is not None and 0 <= index < len(links):
            links.pop(index)
    elif request.form.get("url", "").startswith(("http://", "https://")):
        links.append({"title": request.form.get("title", "").strip(), "url": request.form["url"].strip(),
                      "description": request.form.get("description", "").strip()})
    else:
        flash("Enter a valid http(s) URL.", "danger")
    p.links_json = json.dumps(links)
    db.session.commit()
    return part_page(pid, "links")


@app.post("/parts/<pid>/note")
def part_note(pid):
    db.get_or_404(Part, pid)
    note = Note.query.filter_by(part_id=pid, user_id=g.user.id).first() or Note(part_id=pid, user_id=g.user.id)
    note.text = request.form.get("text", "")
    db.session.add(note)
    db.session.commit()
    flash("Note saved.", "success")
    return part_page(pid, "notes")


@app.post("/parts/<pid>/upload")
def part_upload(pid):
    p = db.get_or_404(Part, pid)
    store_uploads(p)
    p.updated = datetime.utcnow()
    db.session.commit()
    return part_page(pid, request.form.get("tab", "files"))


@app.post("/parts/<pid>/extfile")
def part_extfile(pid):
    p = db.get_or_404(Part, pid)
    files = p.ext_files()
    if request.form["action"] == "delete":
        index = request.form.get("index", type=int)
        if index is not None and 0 <= index < len(files):
            files.pop(index)
    elif request.form.get("url", "").startswith(("http://", "https://")):
        files.append({"title": request.form.get("title", "").strip() or "Datasheet", "url": request.form["url"].strip()})
    else:
        flash("Enter a valid http(s) URL.", "danger")
    p.ext_files_json = json.dumps(files)
    db.session.commit()
    return part_page(pid, "files")


def remove_stored(files):
    for f in files:
        path = os.path.join(UPLOADS, f.stored)
        if os.path.isfile(path):
            os.remove(path)


@app.post("/parts/<pid>/delete")
def part_delete(pid):
    p = db.get_or_404(Part, pid)
    remove_stored(p.files)
    db.session.delete(p)
    db.session.commit()
    flash("Part deleted.", "info")
    return redirect(url_for("parts"))


@app.get("/files/<fid>")
def file(fid):
    f = db.get_or_404(File, fid)
    return send_from_directory(UPLOADS, f.stored, download_name=f.name, as_attachment=request.args.get("download") == "1")


@app.post("/files/<fid>/delete")
def file_delete(fid):
    f = db.get_or_404(File, fid)
    part_id = f.part_id
    remove_stored([f])
    db.session.delete(f)
    db.session.commit()
    return part_page(part_id, "files")


def run_provider(name, q, fresh=False, page=1):
    key = (name, q.lower(), page)
    hit = cache.get(key)
    if hit and hit[0] > time.time() and not fresh:
        return hit[1], hit[2], True
    results = providers.PROVIDERS[name]["search"](provider_config(name), q, page)
    fetched = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    if len(cache) > 500:
        cache.clear()
    cache[key] = (time.time() + CACHE_TTL, results, fetched)
    return results, fetched, False


@app.get("/api/search/<name>")
def api_search(name):
    if name not in enabled_providers():
        abort(404)
    try:
        query = request.args.get("q", "").strip()
        page = max(1, request.args.get("page", 1, type=int))
        key = (name, query.lower(), page)
        if not cache.get(key):
            cached_queries = [k[1] for k in cache if len(k) == 3 and k[0] == name and k[2] == page]
            close = get_close_matches(query.lower(), cached_queries, n=1, cutoff=0.78)
            if close:
                query = close[0]
        results, fetched, cached = run_provider(name, query, page=page)
    except Exception as error:
        app.logger.warning("provider %s failed: %s", name, type(error).__name__)
        return jsonify(error=f"{providers.PROVIDERS[name]['label']}: {error}"), 502
    return jsonify(results=results, fetched=fetched, cached=cached, query=query, page=page,
                   has_next=len(results) >= 20)


@app.get("/api/suggest")
def api_suggest():
    q = request.args.get("q", "").strip()
    words = q.lower().split()
    found = local_query(q).order_by(Part.name).limit(6).all()
    categories = [c for c in Category.query.all() if all(w in c.path.lower() for w in words)][:4]
    return jsonify(
        parts=[{"id": p.id, "name": p.name, "sub": " · ".join(x for x in (p.manufacturer, p.mpn) if x), "img": p.main_image()} for p in found],
        categories=[{"id": c.id, "path": c.path} for c in categories])


@app.get("/api/categories")
def api_categories():
    counts = dict(db.session.query(Part.category_id, db.func.count(Part.id)).group_by(Part.category_id).all())
    return jsonify(sorted(({"id": c.id, "path": c.path, "count": counts.get(c.id, 0)} for c in Category.query.all()), key=lambda c: c["path"].lower()))


def find_existing(mpn, manufacturer):
    if not mpn:
        return None
    for candidate in Part.query.filter(db.func.lower(Part.mpn) == mpn.lower()):
        if not candidate.manufacturer or not manufacturer or normalize(candidate.manufacturer) == normalize(manufacturer):
            return candidate
    return None


def add_to_project(project_id, part_id, qty=1):
    item = Item.query.filter_by(project_id=project_id, part_id=part_id).first()
    if item:
        item.qty += qty
    else:
        db.session.add(Item(project_id=project_id, part_id=part_id, qty=qty))


@app.post("/import")
def import_result():
    data = json.loads(request.form["data"])
    part = find_existing(data.get("mpn"), data.get("manufacturer"))
    created = part is None
    if created:
        part = Part(name=data.get("name") or data.get("mpn") or "Imported part", manufacturer=data.get("manufacturer"),
                    mpn=data.get("mpn"), description=data.get("description"), image_url=data.get("image") or None)
    if not part.specs() and data.get("specs"):
        part.specs_json = json.dumps(data["specs"])
    fetched = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    sources = part.sources()
    for provider in data.get("providers", []):
        sources = [s for s in sources if s["provider"] != provider]
        sources.append({"provider": provider, "fetched": fetched,
                        "offers": [o for o in data.get("offers", []) if o.get("source") == provider]})
    part.sources_json = json.dumps(sources)
    files = part.ext_files()
    for url in data.get("datasheets", []):
        if url.startswith(("http://", "https://")) and url not in [f["url"] for f in files]:
            files.append({"title": "Datasheet", "url": url})
    part.ext_files_json = json.dumps(files)
    db.session.add(part)
    db.session.flush()
    if request.form.get("project"):
        add_to_project(request.form["project"], part.id)
    db.session.commit()
    flash("Imported. Review the details below." if created else "Part already existed; source data was merged.", "success" if created else "info")
    return redirect(url_for("part", pid=part.id))


@app.post("/parts/<pid>/refresh")
def part_refresh(pid):
    p = db.get_or_404(Part, pid)
    names, query = enabled_providers(), p.mpn or p.name
    if not names:
        flash("No providers enabled.", "warning")
        return part_page(pid, "offers")

    configs = {name: provider_config(name) for name in names}

    def fetch(name):
        try:
            key = (name, query.lower(), 1)
            results = providers.PROVIDERS[name]["search"](configs[name], query)
            fetched = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            cache[key] = (time.time() + CACHE_TTL, results, fetched)
            return name, (results, fetched, False)
        except Exception as error:
            return name, error

    with ThreadPoolExecutor(len(names)) as pool:
        outcomes = list(pool.map(fetch, names))
    sources = p.sources()
    for name, outcome in outcomes:
        if isinstance(outcome, Exception):
            flash(f"{providers.PROVIDERS[name]['label']}: {outcome}", "warning")
            continue
        matches = [r for r in outcome[0] if normalize(r["mpn"]) == normalize(query)]
        if matches:
            sources = [s for s in sources if s["provider"] != matches[0]["provider"]]
            sources.append({"provider": matches[0]["provider"], "fetched": outcome[1],
                            "offers": [dict(o, source=m["provider"]) for m in matches for o in m["offers"]]})
    p.sources_json = json.dumps(sources)
    db.session.commit()
    return part_page(pid, "offers")


@app.get("/projects")
def projects():
    return render_template("projects.html", rows=owned_projects())


@app.post("/projects/new")
def project_new():
    project = Project(name=request.form.get("name", "").strip() or "Untitled project",
                      description=request.form.get("description", ""), owner_id=g.user.id,
                      visibility=request.form.get("visibility", "private"),
                      share_token=secrets.token_urlsafe(24) if request.form.get("visibility") == "public" else None)
    db.session.add(project)
    db.session.commit()
    return redirect(url_for("project", pid=project.id))


@app.get("/projects/share/<token>")
def public_project(token):
    project = Project.query.filter_by(share_token=token, visibility="public").first_or_404()
    return render_template("project.html", p=project, can_write=False, public_view=True)


@app.post("/projects/<pid>/share")
def project_share(pid):
    project = db.get_or_404(Project, pid)
    if project.owner_id != g.user.id:
        abort(403)
    email = request.form.get("email", "").strip().lower()
    target = User.query.filter(db.func.lower(User.email) == email).first()
    if not target:
        flash("User not found.", "warning")
    elif target.id == g.user.id:
        flash("You already own this project.", "info")
    else:
        share = ProjectShare.query.filter_by(project_id=pid, user_id=target.id).first() or ProjectShare(project_id=pid, user_id=target.id)
        share.role = request.form.get("role", "read")
        db.session.add(share)
        db.session.commit()
        flash("Project shared.", "success")
    return redirect(url_for("project", pid=pid))


@app.get("/projects/<pid>")
def project(pid):
    project = db.get_or_404(Project, pid)
    if not project_access(project):
        abort(403)
    return render_template("project.html", p=project, can_write=project_access(project, True))


@app.post("/projects/<pid>/edit")
def project_edit(pid):
    project = db.get_or_404(Project, pid)
    if not project_access(project, True):
        abort(403)
    project.name = request.form.get("name", "").strip() or project.name
    project.description = request.form.get("description", "")
    project.visibility = request.form.get("visibility", project.visibility)
    if project.visibility == "public" and not project.share_token:
        project.share_token = secrets.token_urlsafe(24)
    if project.visibility != "public":
        project.share_token = None
    db.session.commit()
    return redirect(url_for("project", pid=pid))


@app.post("/projects/<pid>/add")
def project_add(pid):
    project = db.get_or_404(Project, pid)
    if not project_access(project, True):
        abort(403)
    part_id, name = request.form.get("part_id"), request.form.get("new_name", "").strip()
    if not part_id and name:
        created = Part(name=name)
        db.session.add(created)
        db.session.flush()
        part_id = created.id
    if not part_id:
        abort(400)
    added = db.get_or_404(Part, part_id)
    add_to_project(pid, added.id, max(1, request.form.get("qty", 1, type=int)))
    db.session.commit()
    flash(f"Added {added.name}.", "success")
    return redirect(safe_next(request.form.get("next")) or url_for("project", pid=pid))


@app.post("/items/<iid>")
def item_update(iid):
    item = db.get_or_404(Item, iid)
    if not project_access(item.project, True):
        abort(403)
    if "delete" in request.form:
        db.session.delete(item)
    else:
        item.qty = max(1, request.form.get("qty", 1, type=int))
        item.notes = request.form.get("notes", "")
    db.session.commit()
    return redirect(url_for("project", pid=item.project_id))


@app.post("/projects/<pid>/delete")
def project_delete(pid):
    project = db.get_or_404(Project, pid)
    if project.owner_id != g.user.id:
        abort(403)
    db.session.delete(project)
    db.session.commit()
    return redirect(url_for("projects"))


@app.get("/libraries")
def libraries():
    rows = accessible_libraries()
    return render_template("libraries.html", rows=rows)


@app.post("/libraries/new")
def library_new():
    library = Library(name=request.form.get("name", "").strip() or "Untitled library",
                      description=request.form.get("description", ""), owner_id=g.user.id,
                      visibility=request.form.get("visibility", "private"),
                      share_token=secrets.token_urlsafe(24) if request.form.get("visibility") == "public" else None)
    db.session.add(library)
    db.session.commit()
    return redirect(url_for("library", lid=library.id))


@app.get("/libraries/<lid>")
def library(lid):
    current = db.get_or_404(Library, lid)
    if not library_access(current):
        abort(403)
    query = request.args.get("q", "").strip()
    items = current.items
    if query:
        words = query.lower().split()
        items = [item for item in items if all(any(word in (value or "").lower() for value in
                                                   (item.part.name, item.part.mpn, item.part.manufacturer, item.note))
                                               for word in words)]
        corrected = None
        if not items:
            candidates = [item.part.name for item in current.items] + [item.part.mpn for item in current.items if item.part.mpn]
            close = get_close_matches(query, candidates, n=1, cutoff=0.72)
            if close:
                corrected = close[0]
                items = [item for item in current.items if close[0].lower() in
                         (item.part.name or "").lower() or close[0].lower() in (item.part.mpn or "").lower()]
    else:
        corrected = None
    return render_template("library.html", library=current, library_items=items, search_term=query,
                           search_corrected=corrected, can_write=library_access(current, True))


@app.get("/libraries/share/<token>")
def public_library(token):
    current = Library.query.filter_by(share_token=token, visibility="public").first_or_404()
    return render_template("library.html", library=current, can_write=False, public_view=True)


@app.post("/libraries/<lid>/add")
def library_add(lid):
    current = db.get_or_404(Library, lid)
    if not library_access(current, True):
        abort(403)
    part = db.get_or_404(Part, request.form.get("part_id"))
    item = LibraryPart.query.filter_by(library_id=lid, part_id=part.id).first()
    if not item:
        db.session.add(LibraryPart(library_id=lid, part_id=part.id, category_id=request.form.get("category_id") or part.category_id))
        db.session.commit()
    return redirect(url_for("library", lid=lid))


@app.post("/libraries/<lid>/parts/<item_id>")
def library_part_update(lid, item_id):
    current = db.get_or_404(Library, lid)
    if not library_access(current, True):
        abort(403)
    item = db.get_or_404(LibraryPart, item_id)
    if item.library_id != lid:
        abort(404)
    item.note = request.form.get("note", "")
    item.category_id = request.form.get("category_id") or None
    db.session.commit()
    return redirect(url_for("library", lid=lid))


@app.post("/libraries/<lid>/share")
def library_share(lid):
    current = db.get_or_404(Library, lid)
    if current.owner_id != g.user.id:
        abort(403)
    target = User.query.filter(db.func.lower(User.email) == request.form.get("email", "").strip().lower()).first()
    if not target:
        flash("User not found.", "warning")
    elif target.id != g.user.id:
        share = LibraryShare.query.filter_by(library_id=lid, user_id=target.id).first() or LibraryShare(library_id=lid, user_id=target.id)
        share.role = request.form.get("role", "read")
        db.session.add(share)
        db.session.commit()
        flash("Library shared.", "success")
    return redirect(url_for("library", lid=lid))


@app.get("/categories")
def categories():
    return render_template("categories.html", tree=category_tree())


@app.post("/categories")
def categories_post():
    f, action = request.form, request.form["action"]
    target = db.session.get(Category, f.get("id")) if f.get("id") else None
    if action == "add" and f.get("name", "").strip():
        db.session.add(Category(name=f["name"].strip(), parent_id=f.get("parent_id") or None))
    elif action == "rename" and target and f.get("name", "").strip():
        target.name = f["name"].strip()
    elif action == "move" and target:
        parent_id = f.get("parent_id") or None
        if parent_id in descendant_ids(target.id):
            flash("A category cannot be moved into itself.", "danger")
        else:
            target.parent_id = parent_id
    elif action == "delete" and target:
        Part.query.filter_by(category_id=target.id).update({"category_id": target.parent_id})
        Category.query.filter_by(parent_id=target.id).update({"parent_id": target.parent_id})
        db.session.delete(target)
    db.session.commit()
    return redirect(url_for("categories"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    admin_only()
    if request.method == "POST":
        if request.form["action"] == "provider":
            name = request.form["name"]
            save_setting(name + ".enabled", "1" if request.form.get("enabled") else "0")
            for field in providers.PROVIDERS[name]["fields"]:
                if request.form.get(field):
                    save_setting(f"{name}.{field}", request.form[field].strip())
            db.session.commit()
            flash("Saved.", "success")
        elif request.form["action"] == "admin":
            target = db.get_or_404(User, request.form["id"])
            if target.id != g.user.id:
                target.is_admin = not target.is_admin
                db.session.commit()
        return redirect(url_for("settings"))
    return render_template("settings.html", status={n: provider_config(n) for n in providers.PROVIDERS},
                           meta=providers.PROVIDERS, users=User.query.all(), api_enabled=bool(os.environ.get("API_KEY")))


@app.post("/settings/test/<name>")
def settings_test(name):
    admin_only()
    try:
        providers.PROVIDERS[name]["search"](provider_config(name), "LM358")
        return jsonify(ok=True, message="Connection OK")
    except Exception as error:
        return jsonify(ok=False, message=str(error)[:300])


@app.get("/export")
def export():
    admin_only()
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        database = os.path.join(DATA, "app.db")
        if os.path.isfile(database):
            archive.write(database, "app.db")
        for stored in os.listdir(UPLOADS):
            archive.write(os.path.join(UPLOADS, stored), "uploads/" + stored)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="backup.zip")


def api_guard():
    expected = os.environ.get("API_KEY", "")
    sent = request.headers.get("X-API-Key") or request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
        abort(make_response(jsonify(error="unauthorized"), 401))


def part_dict(p):
    return {
        "id": p.id, "name": p.name, "manufacturer": p.manufacturer, "mpn": p.mpn, "description": p.description,
        "category": {"id": p.category.id, "path": p.category.path} if p.category else None,
        "tags": p.tag_list(), "specs": p.specs(), "links": p.links(), "datasheet_links": p.ext_files(),
        "files": [{"id": f.id, "name": f.name, "kind": f.kind, "size": f.size,
                   "url": url_for("api_v1_file", fid=f.id, _external=True)} for f in p.files],
        "offers": p.sources(), "created": p.created.isoformat(), "updated": p.updated.isoformat(),
        "url": url_for("part", pid=p.id, _external=True),
    }


@app.get("/api/v1/parts")
def api_v1_parts():
    api_guard()
    limit = min(request.args.get("limit", 50, type=int), 100)
    query = local_query(request.args.get("q", ""), request.args.get("category"), request.args.get("manufacturer"), request.args.get("tag"))
    rows = query.order_by(Part.name).offset(request.args.get("offset", 0, type=int)).limit(limit).all()
    return jsonify(total=query.count(), parts=[part_dict(p) for p in rows])


@app.get("/api/v1/parts/<pid>")
def api_v1_part(pid):
    api_guard()
    return jsonify(part_dict(db.get_or_404(Part, pid)))


@app.get("/api/v1/categories")
def api_v1_categories():
    api_guard()
    return jsonify(category_tree())


@app.get("/api/v1/files/<fid>")
def api_v1_file(fid):
    api_guard()
    f = db.get_or_404(File, fid)
    return send_from_directory(UPLOADS, f.stored, download_name=f.name)
