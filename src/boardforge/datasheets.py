"""Part-fact cache (§4.10 gate 3): datasheet PDFs and their structured extractions.

Layout is kicad-happy's (``skills/datasheets/references/cache-layout.md``), used twice:

    <project>/datasheets/            the project's copy (what the analyzers and review read)
    ~/.cache/boardforge/datasheets/  shared by all projects ([datasheets] cache)
        manifest.json                {"pdfs": {"sha256:<hex>": {path, mpns, source_url}}}
        <MPN>.pdf
        extracted/<MPN>.json         one fact envelope per MPN (+ .plan.json/.scout.json)

``sync`` fills the project from the shared cache, then finds missing PDFs (local search
directories, the symbol's datasheet URL, LCSC); ``add`` takes one PDF by path or URL.
``publish`` copies new extractions back. Extraction itself is LLM work (an agent following
kicad-happy's prompts and schemas); ``check`` validates its result and fills in the derived
fields, ``summary`` renders it for coders.
"""

import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

from boardforge.analysis import VENDOR

SCHEMAS = os.path.join(VENDOR, "datasheets", "schemas")
LCSC_FETCH = os.path.join(VENDOR, "lcsc", "scripts", "fetch_datasheet_lcsc.py")
MPN_FIELDS = ("mpn", "mfr_pn", "mfr part number", "manufacturer_part_number", "manufacturer part number")
SIDE_FILES = (".plan.json", ".scout.json", ".variant.json")
SKILL = os.path.join(VENDOR, "datasheets")  # kicad-happy's extraction prompts, schemas, scripts


def kh(module):
    """Import one of kicad-happy's datasheets/scripts modules."""
    import importlib.util

    d = os.path.join(SKILL, "scripts")
    sys.path.insert(0, d)
    try:
        spec = importlib.util.spec_from_file_location("kh_" + module, os.path.join(d, module + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(d)
    return mod


def sanitize(mpn):
    """kicad-happy's file name for an MPN (datasheet_lookup.sanitize_mpn)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", mpn.strip())


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def is_pdf(path):
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def mpn_of(comp):
    for k, v in comp.get("fields", {}).items():
        if k.lower() in MPN_FIELDS and str(v).strip():
            return str(v).strip()
    return None


def ref_prefix(ref):
    return re.match(r"[A-Za-z]*", ref).group(0)


def parts(netlist, prefixes):
    """{mpn: {refs, value, footprint, url, wanted}} from netlist.json. ``wanted``: some ref
    has one of ``prefixes`` (the parts whose facts the checks use; passives are not)."""
    out = {}
    for c in netlist.get("components", []):
        mpn = mpn_of(c)
        if not mpn:
            continue
        p = out.setdefault(mpn, {"refs": [], "value": c.get("value"), "footprint": c.get("footprint"),
                                 "url": None, "wanted": False})
        p["refs"].append(c["ref"])
        p["wanted"] |= ref_prefix(c["ref"]) in prefixes
        url = netlist.get("symbols", {}).get(c.get("lib_id"), {}).get("datasheet") or ""
        if url.startswith("http") and not p["url"]:
            p["url"] = url
    return out


class Store:
    """One datasheets/ directory: PDFs, manifest.json, extracted/."""

    def __init__(self, root):
        self.root = root
        self.extracted = os.path.join(root, "extracted")
        self._manifest = None

    @property
    def manifest(self):
        if self._manifest is None:
            path = os.path.join(self.root, "manifest.json")
            self._manifest = {"version": "1.0", "pdfs": {}}
            if os.path.exists(path):
                with open(path) as f:
                    self._manifest = json.load(f)
                self._manifest.setdefault("pdfs", {})
        return self._manifest

    def save(self):
        if self._manifest is None:
            return
        os.makedirs(self.root, exist_ok=True)
        self._manifest["last_updated"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(os.path.join(self.root, "manifest.json"), "w") as f:
            json.dump(self._manifest, f, indent=1)

    def extraction_file(self, mpn):
        return os.path.join(self.extracted, sanitize(mpn) + ".json")

    def extraction(self, mpn):
        try:
            with open(self.extraction_file(mpn)) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def extracted_mpns(self):
        """MPNs with an extraction here (from source.mpn; else the file name)."""
        out = []
        for f in sorted(glob.glob(os.path.join(self.extracted, "*.json"))):
            if f.endswith(SIDE_FILES) or os.path.basename(f) in ("manifest.json", "index.json"):
                continue
            try:
                with open(f) as fh:
                    d = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            out.append(((d.get("source") or {}).get("mpn") or os.path.basename(f)[:-5]))
        return out

    def _rel(self, path):
        """A local_path from an extraction or manifest, relative to this store."""
        if not path:
            return None
        path = path[len("datasheets/"):] if path.startswith("datasheets/") else path
        return os.path.join(self.root, path)

    def pdf(self, mpn):
        """Path of this MPN's PDF here, or None."""
        for e in self.manifest["pdfs"].values():
            if mpn in e.get("mpns", []) and os.path.exists(self._rel(e["path"])):
                return self._rel(e["path"])
        ex = self.extraction(mpn)
        cands = [self._rel((ex or {}).get("source", {}).get("local_path")),
                 os.path.join(self.root, sanitize(mpn) + ".pdf")]
        return next((c for c in cands if c and os.path.exists(c)), None)

    def stale(self, mpn):
        """Why this MPN's extraction no longer matches its PDF, or None (kicad-happy's rule)."""
        ex = self.extraction(mpn)
        if ex is None:
            return None
        src = ex.get("source") or {}
        pdf = self._rel(src.get("local_path"))
        if not pdf or not os.path.exists(pdf):
            return "pdf_missing"
        if src.get("sha256") and sha256(pdf) != src["sha256"]:
            return "pdf_hash_mismatch"
        return None

    def add_pdf(self, src, mpn, url=None, name=None):
        """Copy a PDF in (deduplicated by SHA) and record it for ``mpn``. Returns its path."""
        digest = sha256(src)
        entry = self.manifest["pdfs"].get(digest)
        if entry and os.path.exists(self._rel(entry["path"])):
            if mpn not in entry["mpns"]:
                entry["mpns"].append(mpn)
            return self._rel(entry["path"])
        name = name or sanitize(mpn) + ".pdf"
        dst = os.path.join(self.root, name)
        os.makedirs(self.root, exist_ok=True)
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copyfile(src, dst)
        self.manifest["pdfs"][digest] = {"path": name, "mpns": [mpn], "source_url": url}
        return dst

    def copy_extraction(self, other, mpn):
        """Copy this MPN's extraction, its side files and its PDF from store ``other``."""
        os.makedirs(self.extracted, exist_ok=True)
        base = sanitize(mpn)
        for suffix in (".json",) + SIDE_FILES:
            f = os.path.join(other.extracted, base + suffix)
            if os.path.exists(f):
                shutil.copyfile(f, os.path.join(self.extracted, base + suffix))
        pdf = other.pdf(mpn)
        if pdf:
            src = (other.extraction(mpn) or {}).get("source") or {}
            name = os.path.relpath(pdf, other.root)
            got = os.path.relpath(self.add_pdf(pdf, mpn, src.get("source_url"), name=name), self.root)
            if got != name:  # the same PDF was already here under another name
                f = self.extraction_file(mpn)
                ex = self.extraction(mpn)
                ex["source"]["local_path"] = got
                with open(f, "w") as fh:
                    json.dump(ex, fh, indent=1)


def stores(proj):
    cfg = proj.cfg["datasheets"]
    project = Store(os.path.join(proj.root, cfg["dir"]))
    shared = Store(os.path.expanduser(cfg["cache"])) if cfg["cache"] else None
    return project, shared


def project_parts(proj):
    """parts() of the built design (build/netlist.json), else of the sourced BOM CSV
    ([review] bom), so datasheets can be gathered before there is any circuit code."""
    prefixes = set(proj.cfg["datasheets"]["prefixes"])
    path = os.path.join(proj.out, "netlist.json")
    if os.path.exists(path):
        with open(path) as f:
            return parts(json.load(f), prefixes)
    bom = os.path.join(proj.root, proj.cfg.get("review", {}).get("bom", "sourcing/sourced_bom.csv"))
    if not os.path.exists(bom):
        raise FileNotFoundError("no netlist (run `boardforge sch`) and no sourced BOM "
                                f"({os.path.relpath(bom, proj.root)})")
    import csv

    from boardforge.review.bom_check import expand_refdes

    comps = []
    with open(bom, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row = {k.strip().casefold(): (v or "").strip() for k, v in row.items() if k}
            comps += [{"ref": r, "value": row.get("value"), "footprint": row.get("footprint"),
                       "fields": {"MPN": row.get("mpn", "")}} for r in expand_refdes(row.get("refdes"))]
    return parts({"components": comps}, prefixes)


def quality(ex):
    return ((ex or {}).get("extraction") or {}).get("quality_score")


def validate(ex):
    """Schema problems in an extraction (kicad-happy's extraction.schema.json). Returns
    (problems, checked); checked is False when jsonschema isn't installed."""
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError:
        return [], False
    reg = Registry()
    for p in glob.glob(os.path.join(SCHEMAS, "*.schema.json")):
        with open(p) as f:
            s = json.load(f)
        reg = reg.with_resource(s["$id"], Resource.from_contents(s))
    with open(os.path.join(SCHEMAS, "extraction.schema.json")) as f:
        v = jsonschema.Draft202012Validator(json.load(f), registry=reg)
    return [f"{'/'.join(map(str, e.absolute_path)) or '(top)'}: {e.message[:120]}"
            for e in v.iter_errors(ex)][:5], True


def status(proj):
    """One row per MPN: where its PDF and extraction are, staleness, quality."""
    project, shared = stores(proj)
    rows = []
    for mpn, p in sorted(project_parts(proj).items()):
        ex = project.extraction(mpn)
        rows.append({"mpn": mpn, "refs": p["refs"], "wanted": p["wanted"],
                     "pdf": bool(project.pdf(mpn)), "extracted": ex is not None,
                     "quality": quality(ex), "stale": project.stale(mpn),
                     "in_cache": bool(shared and shared.extraction(mpn) is not None)})
    return rows


def _search_dirs(proj):
    return [os.path.join(proj.root, os.path.expanduser(d)) for d in proj.cfg["datasheets"]["search"]]


def find_local(mpn, dirs):
    """A PDF whose name is the longest prefix (4+ characters) of the MPN, ignoring case and
    punctuation: OPA810.pdf for OPA810IDR."""
    key = re.sub(r"[^a-z0-9]", "", mpn.lower())
    best = None
    for d in dirs:
        for f in glob.glob(os.path.join(d, "*.pdf")) + glob.glob(os.path.join(d, "*.PDF")):
            stem = re.sub(r"[^a-z0-9]", "", os.path.splitext(os.path.basename(f))[0].lower())
            if len(stem) >= 4 and key.startswith(stem) and (best is None or len(stem) > best[0]):
                best = (len(stem), f)
    return best[1] if best else None


def _key(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def identify(pdf, mpn, pages=5):
    """Is this PDF the MPN's datasheet? True: its first pages name the part (at least its
    core: letters then digits, AP2112 for AP2112K-1.2TRG1, W25Q32 for W25Q32JVSSIQ);
    False: they have text but don't; None: no text to check (scanned, or no pdftotext)."""
    if not shutil.which("pdftotext"):
        return None
    try:
        r = subprocess.run(["pdftotext", "-l", str(pages), pdf, "-"], capture_output=True,
                           text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    text = _key(r.stdout)
    if len(text) < 200:
        return None
    key = _key(mpn)
    core = re.match(r"\d*[A-Z]+\d+", key)
    if core and len(core.group()) < 4:
        core = re.match(r"\d*[A-Z]+\d+[A-Z]+\d+", key) or core
    need = len(core.group()) if core else min(len(key), 5)
    return any(key[:n] in text for n in range(len(key), need - 1, -1))


def fetch(mpn, url, dest, timeout=90, search=True, keep=False):
    """Download a PDF with kicad-happy's LCSC fetcher: the URL first, then (``search``) an
    LCSC search. A PDF is kept only if its text names the part: symbol URLs often cover a
    sibling part, LCSC's search is fuzzy, and an image-only PDF can't be checked. Returns
    (source url or "lcsc", None) or (None, why). ``keep`` keeps an unconfirmed PDF."""
    tries = ([("url", [url])] if url else []) + ([("lcsc", ["--search", mpn])] if search else [])
    why = []
    for name, args in tries:
        try:
            r = subprocess.run([sys.executable, LCSC_FETCH, *args, "--output", dest],
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            why.append(f"{name}: timeout")
            continue
        ok = r.returncode == 0 and is_pdf(dest)
        who = identify(dest, mpn) if ok else None
        if who or (ok and keep):  # keep: the caller vouches for it (add --force)
            return (url if name == "url" else "lcsc"), None
        if ok:
            # A download has to prove it is the part: image-only PDFs (no text) let two
            # wrong-part files in (an LCSC crystal sheet filed as an ESD diode).
            why.append(f"{name}: got a PDF that " + ("doesn't name " + mpn if who is False else
                                                      "has no text to confirm it is " + mpn))
        else:
            why.append(f"{name}: exit {r.returncode} {(r.stderr or r.stdout).strip()[-120:]}")
        if os.path.exists(dest):
            os.remove(dest)
    return None, "; ".join(why)


def sync(proj, fresh=False, download=True, log=print):
    """Fill <project>/datasheets for the wanted MPNs. Returns a summary."""
    project, shared = stores(proj)
    use_cache = shared and not fresh and os.path.abspath(shared.root) != os.path.abspath(project.root)
    dirs = _search_dirs(proj)
    done = {"from_cache": [], "pdf_local": [], "pdf_downloaded": [], "pdf_missing": [], "to_extract": []}
    for mpn, p in sorted(project_parts(proj).items()):
        if not p["wanted"]:
            continue
        if use_cache and project.extraction(mpn) is None and shared.extraction(mpn) is not None:
            if shared.stale(mpn):
                log(f"! {mpn}: shared extraction is stale ({shared.stale(mpn)}); not copied")
            else:
                project.copy_extraction(shared, mpn)
                done["from_cache"].append(mpn)
        if not project.pdf(mpn):
            src = (shared.pdf(mpn) if use_cache else None) or find_local(mpn, dirs)
            if src:
                if identify(src, mpn) is False:
                    log(f"! {mpn}: {os.path.basename(src)} doesn't name the part; using it anyway")
                project.add_pdf(src, mpn)
                done["pdf_local"].append(mpn)
            elif download:
                tmp = os.path.join(project.root, sanitize(mpn) + ".pdf.part")
                os.makedirs(project.root, exist_ok=True)
                origin, why = fetch(mpn, p["url"], tmp)
                if origin:
                    project.add_pdf(tmp, mpn, origin if origin != "lcsc" else None, name=sanitize(mpn) + ".pdf")
                    os.remove(tmp)
                    done["pdf_downloaded"].append(mpn)
                else:
                    log(f"! {mpn}: no PDF ({why})")
                    done["pdf_missing"].append(mpn)
            else:
                done["pdf_missing"].append(mpn)
        if project.pdf(mpn) and (project.extraction(mpn) is None or project.stale(mpn)):
            done["to_extract"].append(mpn)
    project.save()
    return done


def add(proj, mpn, src, force=False):
    """Put one PDF (a file or URL) into the project's datasheets for ``mpn``, after checking
    it is a PDF whose text names the part (``force`` skips the name check, for a scanned
    datasheet checked by eye). Returns its path."""
    project, _ = stores(proj)
    os.makedirs(project.root, exist_ok=True)
    url = src if re.match(r"https?://", src) else None
    if url:
        tmp = os.path.join(project.root, sanitize(mpn) + ".pdf.part")
        origin, why = fetch(mpn, url, tmp, search=False, keep=force)
        if not origin:
            raise ValueError(f"{mpn}: {why}")
        src = tmp
    elif not is_pdf(src):
        raise ValueError(f"{src} is not a PDF")
    elif not force:
        who = identify(src, mpn)
        if who is False:
            raise ValueError(f"{src} doesn't name {mpn} in its first pages (--force to add it anyway)")
        if who is None:
            raise ValueError(f"{src} has no text to confirm it is {mpn} (scanned?); check it by eye, "
                             "then add it with --force")
    path = project.add_pdf(src, mpn, url, name=sanitize(mpn) + ".pdf")
    if url and os.path.exists(src):
        os.remove(src)
    project.save()
    return path


def _page_count(pdf):
    try:
        out = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True, timeout=30).stdout
        return int(next(line.split()[-1] for line in out.splitlines() if line.startswith("Pages:")))
    except (OSError, StopIteration, ValueError, subprocess.TimeoutExpired):
        return None


def check(proj, mpns):
    """Validate extractions an agent wrote and fill in what can be derived: source.sha256,
    local_path and page_count from the project's PDF, schema_version, extraction.extracted_at
    and quality_score (kicad-happy's v1.4 score). Rewrites each file; returns
    [{mpn, quality, problems, issues}] (schema problems, kicad-happy consistency issues)."""
    project, _ = stores(proj)
    score, verify = kh("datasheet_score"), kh("datasheet_verify")
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for mpn in mpns:
        ex = project.extraction(mpn)
        if ex is None:
            rows.append({"mpn": mpn, "quality": None, "problems": [
                f"no {os.path.relpath(project.extraction_file(mpn), proj.root)} (or not valid JSON)"], "issues": []})
            continue
        src = ex.setdefault("source", {})
        src.setdefault("mpn", mpn)
        pdf = project.pdf(mpn)
        if pdf:
            src["sha256"] = sha256(pdf)
            src["local_path"] = os.path.relpath(pdf, project.root)
            src["page_count"] = _page_count(pdf) or src.get("page_count")
        cats = ex.get("categories") or []
        ex.setdefault("schema_version", {"base": "1.0", "categories": {}})
        ex["schema_version"].setdefault("categories", {})
        for c in cats:
            ex["schema_version"]["categories"].setdefault(c, "1.0")
        meta = ex.setdefault("extraction", {})
        for k, v in (("extracted_at", now), ("extractor_schema_version", "1.0"), ("extractor_scout", None),
                     ("plan_ref", None)):
            meta.setdefault(k, v)
        meta["quality_score"] = score.score_v14_extraction(ex)["score"]
        with open(project.extraction_file(mpn), "w") as f:
            json.dump(ex, f, indent=1)
        problems, checked = validate(ex)
        if not checked:
            problems = ["jsonschema not installed: schema not checked"]
        issues = [f"{i['path']}: {i['description']}" for i in verify.verify_v14_extraction(ex)]
        if not pdf:
            issues.append("no PDF in the project's datasheets: facts can't be traced to a document")
        rows.append({"mpn": mpn, "quality": meta["quality_score"], "problems": problems, "issues": issues})
    return rows


def publish(proj, force=False, log=print):
    """Copy the project's extractions into the shared cache. An entry already there is kept
    unless the project's is newer and scores at least as well (or ``force``)."""
    project, shared = stores(proj)
    if not shared:
        raise ValueError("no shared cache: [datasheets] cache is empty")
    out = {"published": [], "kept": [], "rejected": []}
    for mpn in project.extracted_mpns():
        ex = project.extraction(mpn)
        problems, checked = validate(ex)
        if project.stale(mpn):
            problems.append(f"stale ({project.stale(mpn)})")
        if problems:
            log(f"! {mpn}: not published: {'; '.join(problems)}")
            out["rejected"].append(mpn)
            continue
        if not checked:
            log(f"! {mpn}: schema not checked (pip install jsonschema referencing)")
        old = shared.extraction(mpn)
        if old and not force:
            newer = (ex.get("extraction") or {}).get("extracted_at", "") > \
                    (old.get("extraction") or {}).get("extracted_at", "")
            if not newer or (quality(ex) or 0) < (quality(old) or 0):
                out["kept"].append(mpn)
                continue
        shared.copy_extraction(project, mpn)
        out["published"].append(mpn)
    shared.save()
    return out


def _best(specs, scale=1.0):
    """typ, else min, of the first SpecValue that has one; (value, page) or (None, None)."""
    for s in specs or []:
        v = s.get("typ") if s.get("typ") is not None else s.get("min")
        if v is not None:
            return v * scale, (s.get("evidence") or {}).get("page")
    return None, None


def amp_facts(store, mpn):
    """Amplifier specs for the SPICE path gate from an extraction's opamp section:
    {gbw_hz, slew_vus, source} (only what the datasheet gave), or None."""
    ex = store.extraction(mpn)
    op = (ex or {}).get("opamp")
    if not op:
        return None
    out = {}
    gbw, page = _best(op.get("gbw"))
    if gbw:
        out["gbw_hz"] = gbw
        out["source"] = f"{mpn} datasheet p{page}" + (" (stale)" if store.stale(mpn) else "")
    slew, _ = _best(op.get("slew_rate"), 1e-6)
    if slew:
        out["slew_vus"] = slew
    return out or None


def _spec(specs):
    """'min / typ / max unit (condition) p<page>' for the first SpecValue of a list."""
    for v in specs or []:
        if not isinstance(v, dict):
            continue
        nums = " / ".join("-" if v.get(k) is None else f"{v[k]:g}" for k in ("min", "typ", "max"))
        cond = f" ({v['condition']})" if v.get("condition") else ""
        page = (v.get("evidence") or {}).get("page")
        return f"{nums} {v.get('unit') or ''}{cond}" + (f" p{page}" if page else "")
    return None


def summary(ex):
    """Markdown view of an extraction for coders and SUMMARY.md: package, ratings, pinout,
    category facts. Generated, so the summary's tables can't drift from the extraction."""
    src, base = ex.get("source") or {}, ex.get("base") or {}
    pkg = base.get("package") or {}
    q = (ex.get("extraction") or {}).get("quality_score")
    out = [f"# {src.get('mpn', '?')} - {base.get('description') or base.get('family', '')}", "",
           f"{src.get('manufacturer', '')}, datasheet {src.get('local_path') or src.get('source_url') or '-'}"
           f"{', quality ' + str(q) if q is not None else ''}", "",
           f"Package: {pkg.get('code', '?')}, {pkg.get('pin_count') or '?'} pins, pitch "
           f"{pkg.get('pitch_mm') or '?'} mm, thermal pad {pkg.get('thermal_pad')}", ""]
    for title, key in (("Absolute maximum", "absolute_max"), ("Recommended operating", "recommended_operating")):
        rows = [(k, _spec(v)) for k, v in (base.get(key) or {}).items() if _spec(v)]
        if rows:
            out += [f"## {title} (min / typ / max)", ""] + [f"- {k}: {v}" for k, v in rows] + [""]
    pins = base.get("pinout") or []
    if pins:
        out += ["## Pinout", "", "| Pin | Name | Type | Domain | Function |", "|---|---|---|---|---|"]
        for p in pins:
            out.append(f"| {','.join(str(n) for n in p.get('numbers') or [])} | {p.get('name', '')} | "
                       f"{p.get('type', '')} | {p.get('power_domain') or ''} | {p.get('description') or ''} |")
        out.append("")
    for cat in ex.get("categories") or []:
        block = ex.get(cat) or {}
        rows = []
        for k, v in block.items():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "unit" in v[0]:
                if _spec(v):
                    rows.append(f"- {k}: {_spec(v)}")
            elif isinstance(v, (str, int, float, bool)) and v is not None:
                rows.append(f"- {k}: {v}")
        if rows:
            out += [f"## {cat}", ""] + rows + [""]
    return "\n".join(out)


def expose(proj):
    """kicad-happy's analyzers look for datasheets/ next to the schematic or one level up:
    link <out>/datasheets to the project's copy. Returns a warning or None."""
    project, _ = stores(proj)
    link = os.path.join(proj.out, "datasheets")
    if not os.path.isdir(project.root) or os.path.realpath(link) == os.path.realpath(project.root):
        return None
    try:
        if os.path.islink(link):
            os.remove(link)
        elif os.path.exists(link):
            return f"{link} exists and is not a link; the analyzers read it, not {project.root}"
        os.symlink(os.path.relpath(project.root, proj.out), link)
    except OSError as e:
        return f"could not link {link}: {e}"
    return None
