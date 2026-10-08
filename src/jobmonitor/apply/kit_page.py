# ruff: noqa: E501  (the page's inline CSS and JS read better unwrapped)
"""The Apply kit page: server-rendered HTML for a phone, no build step.

Every value is escaped; the only script is the small inline one below (copy to
clipboard, draft/save calls, step navigation). The page talks only to its own
``/kit/{job_id}/...`` endpoints with the same signed token it was opened with.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Sequence

from jobmonitor.apply.questions import FILE, LONG, SELECT
from jobmonitor.apply.service import FilledQuestion, Kit
from jobmonitor.apply.store import DRAFT, LIBRARY, PROFILE, USER, Answer

_BOLD = re.compile(r"\*\*(.+?)\*\*")

SOURCE_LABELS = {
    PROFILE: "from your profile",
    LIBRARY: "your saved answer",
    USER: "your answer",
    DRAFT: "draft from your resume - edit before using",
}


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _rich(text: str) -> str:
    """Escape, then turn ``**bold**`` into <strong> (tips only)."""
    return _BOLD.sub(r"<strong>\1</strong>", _e(text))


def _copy_button(target: str) -> str:
    return f'<button type="button" class="btn small" data-copy="{_e(target)}">Copy</button>'


def _answer_editor(uid: str, question: str, answer: Answer | None, *, draft: bool) -> str:
    """An editable answer: textarea + Copy / Save / Redraft."""
    text = answer.text if answer else ""
    source = SOURCE_LABELS.get(answer.source, "") if answer else ""
    pending = ' data-autodraft="1"' if draft and answer is None else ""
    placeholder = "Drafting from your resume..." if pending else "Write your answer"
    return (
        f'<div class="editor" data-question="{_e(question)}"{pending}>'
        f'<textarea id="{uid}" rows="5" placeholder="{_e(placeholder)}">{_e(text)}</textarea>'
        f'<div class="meta" data-source>{_e(source)}</div>'
        '<div class="actions">'
        f"{_copy_button(uid)}"
        '<button type="button" class="btn small" data-save>Save</button>'
        '<label class="lib"><input type="checkbox" data-library> reuse next time</label>'
        '<button type="button" class="btn small ghost" data-redraft>'
        f"{'Redraft' if text or pending else 'Draft from resume'}</button>"
        "</div></div>"
    )


def _question(fq: FilledQuestion, uid: str) -> str:
    q, answer = fq.question, fq.answer
    star = ' <span class="req" aria-label="required">*</span>' if q.required else ""
    parts = [f'<div class="q"><div class="label">{_e(q.label)}{star}</div>']
    if q.hint:
        parts.append(f'<div class="hint">{_rich(q.hint)}</div>')
    if q.kind == FILE:
        parts.append("</div>")
        return "".join(parts)
    if q.kind == LONG or (
        answer is not None and answer.source in {DRAFT, USER} and len(answer.text) > 80
    ):
        # Required free text is drafted as the page opens; optional ones wait for
        # a tap on Redraft, so nothing is spent on questions the user may skip.
        parts.append(_answer_editor(uid, q.label, answer, draft=q.draftable and q.required))
    elif answer is not None and q.kind == SELECT:
        # A dropdown is picked, not pasted: show the option to choose, no button.
        parts.append(
            f'<div class="value"><span class="pick">Select: {_e(answer.text)}</span></div>'
        )
    elif answer is not None:
        # Profile values are the default and need no note; a saved answer says so.
        note = "" if answer.source == PROFILE else SOURCE_LABELS.get(answer.source, "")
        parts.append(
            f'<div class="value"><span id="{uid}" data-value="{_e(answer.text)}">{_e(answer.text)}</span>'
            f"{_copy_button(uid)}</div>" + (f'<div class="meta">{_e(note)}</div>' if note else "")
        )
    elif q.kind == SELECT and q.options and len(q.options) <= 6:
        # Tap the option you'll pick; it's remembered for this job (and, with
        # "reuse next time", for every form asking the same question).
        chips = "".join(
            f'<button type="button" class="chip" data-choice="{_e(option)}">{_e(option)}</button>'
            for option in q.options
        )
        parts.append(
            f'<div class="editor choice" data-question="{_e(q.label)}">{chips}'
            '<label class="lib"><input type="checkbox" data-library> reuse next time</label>'
            '<div class="meta" data-source></div></div>'
        )
    else:
        if q.options:
            parts.append(f'<div class="hint">Options: {_e(" / ".join(q.options))}</div>')
        parts.append(_short_editor(uid, q.label))
    parts.append("</div>")
    return "".join(parts)


def _short_editor(uid: str, question: str) -> str:
    """One line for a short answer the profile doesn't hold: type it, save it."""
    return (
        f'<div class="editor short" data-question="{_e(question)}">'
        f'<input id="{uid}" type="text" placeholder="Your answer" autocomplete="off">'
        '<div class="meta" data-source></div>'
        '<div class="actions">'
        f"{_copy_button(uid)}"
        '<button type="button" class="btn small" data-save>Save</button>'
        '<label class="lib"><input type="checkbox" data-library> reuse next time</label>'
        "</div></div>"
    )


def _skip(fq: FilledQuestion) -> bool:
    """An optional field the profile leaves empty (a minor, a website): not shown."""
    q = fq.question
    return fq.answer is None and q.profile_key is not None and not q.required and q.kind != LONG


def render(kit: Kit, *, token: str, base_path: str) -> str:
    job = kit.job
    endpoints = {
        "draft": f"{base_path}/draft?t={token}",
        "answers": f"{base_path}/answers?t={token}",
    }
    nav = "".join(
        f'<a href="#step-{i}" data-step="{i}">{i}. {_e(fs.step.title)}</a>'
        for i, fs in enumerate(kit.steps, start=1)
    )
    resume = (
        f'<a class="btn" href="{_e(base_path)}/resume?t={_e(token)}">Download resume</a>'
        '<div class="hint">Save it to Files once; every application can then attach it.</div>'
        if kit.resume_available
        else '<div class="hint">No resume uploaded yet: run <code>make apply-setup</code>.</div>'
    )
    steps_html = []
    counter = 0
    for i, fs in enumerate(kit.steps, start=1):
        rows = []
        for fq in fs.questions:
            if _skip(fq):
                continue
            counter += 1
            rows.append(_question(fq, f"a{counter}"))
        paste = (
            '<div class="paste"><div class="label">Another question on this page?</div>'
            '<textarea rows="2" data-paste placeholder="Paste the question here"></textarea>'
            '<button type="button" class="btn small" data-paste-draft>Draft answer</button>'
            '<button type="button" class="btn small ghost" data-paste-write>Write my own</button>'
            "</div>"
            if fs.step.paste_box
            else ""
        )
        tip = f'<p class="tip">{_rich(fs.step.tip)}</p>' if fs.step.tip else ""
        steps_html.append(
            f'<details class="step" id="step-{i}"{" open" if i == 1 else ""}>'
            f"<summary><span>{i}</span> {_e(fs.step.title)}</summary>"
            f"{tip}{''.join(rows)}{paste}</details>"
        )
    extra_html = "".join(
        f'<div class="q"><div class="label">{_e(a.question)}</div>'
        f"{_answer_editor(f'x{n}', a.question, a, draft=False)}</div>"
        for n, a in enumerate(kit.extra, start=1)
    )
    extra_block = (
        f'<section class="card" id="extra"><h2>Your other answers</h2>{extra_html}</section>'
        if kit.extra
        else '<section class="card" id="extra" hidden><h2>Your other answers</h2></section>'
    )
    top_tip = f'<p class="tip">{_rich(kit.walkthrough.tip)}</p>' if kit.walkthrough.tip else ""
    where = f" · {_e(job.location)}" if job.location else ""
    # Inside <script> entities are not decoded, so the JSON is made safe instead of
    # escaped: no "</script>" can appear in it.
    config = json.dumps(endpoints).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<title>Apply kit · {_e(job.company)}</title>
<style>{CSS}</style></head>
<body>
<header class="card">
  <div class="company">{_e(job.company)}</div>
  <h1>{_e(job.title)}</h1>
  <div class="hint">{_e(kit.walkthrough.ats.title())} application{where}</div>
  <a class="btn primary" href="{_e(job.url)}" target="_blank" rel="noopener noreferrer">Open application</a>
  {top_tip}
</header>
<nav class="steps" aria-label="Form pages">{nav}</nav>
<section class="card"><h2>Resume</h2>{resume}</section>
{"".join(steps_html)}
{extra_block}
<p class="foot">Nothing is submitted for you: paste, check, and submit on the form yourself.</p>
<script type="application/json" id="kit-config">{config}</script>
<script>{JS}</script>
</body></html>"""


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#16181d;--muted:#5f6673;--line:#e3e6eb;
--accent:#1a56db;--accent-text:#fff;--tip:#eef4ff;--ok:#127a3e}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--text:#eceef2;
--muted:#9aa2b1;--line:#2a2f38;--accent:#5b8cff;--accent-text:#0b0d10;--tip:#1b2433;--ok:#4cc38a}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;padding:12px 16px 40px;background:var(--bg);color:var(--text);
font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.card,.step{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:14px 16px;margin:0 0 12px}
h1{font-size:20px;margin:2px 0 4px}h2{font-size:16px;margin:0 0 8px}
.company{color:var(--muted);font-weight:600;font-size:14px}
.hint,.meta{color:var(--muted);font-size:13px;margin:2px 0}
.meta{font-style:italic}.req{color:#d33}
.tip{background:var(--tip);border-radius:10px;padding:10px 12px;margin:10px 0 4px;font-size:14px}
.btn{display:inline-block;border:1px solid var(--line);background:var(--card);color:var(--text);
border-radius:10px;padding:10px 14px;font:inherit;text-decoration:none;cursor:pointer;margin:6px 6px 0 0}
.btn.primary{background:var(--accent);color:var(--accent-text);border-color:var(--accent);
font-weight:600;display:block;text-align:center;margin-top:12px}
.btn.small{padding:7px 12px;font-size:14px}.btn.ghost{background:transparent}
.btn.done{color:var(--ok);border-color:var(--ok)}
.steps{position:sticky;top:0;z-index:2;display:flex;gap:6px;overflow-x:auto;padding:8px 0;
margin:0 -16px 8px;padding-left:16px;background:var(--bg)}
.steps a{white-space:nowrap;font-size:13px;color:var(--text);text-decoration:none;
border:1px solid var(--line);background:var(--card);border-radius:999px;padding:6px 10px}
summary{font-weight:600;font-size:17px;cursor:pointer;list-style:none}
summary::-webkit-details-marker{display:none}
summary span{display:inline-block;width:24px;height:24px;border-radius:50%;background:var(--accent);
color:var(--accent-text);text-align:center;font-size:14px;line-height:24px;margin-right:6px}
.q{border-top:1px solid var(--line);padding:7px 0}.q:first-of-type{border-top:0}
.label{font-weight:600;font-size:14px;color:var(--muted)}
.pick{font-weight:600}
.value .btn{margin:0;flex-shrink:0;white-space:nowrap}
.value{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:2px;
word-break:break-word;font-size:16px}
textarea,.short input[type=text]{width:100%;font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);
border-radius:10px;padding:10px;margin-top:6px;resize:vertical}
.actions{display:flex;flex-wrap:wrap;align-items:center}
.lib{font-size:13px;color:var(--muted);margin:6px 8px 0 2px}
.paste{border-top:1px dashed var(--line);margin-top:8px;padding-top:10px}
.chip{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:999px;padding:8px 16px;font:inherit;margin:6px 6px 0 0;cursor:pointer}
.chip.on{background:var(--accent);color:var(--accent-text);border-color:var(--accent)}
.foot{color:var(--muted);font-size:13px;text-align:center}
code{font-size:13px}
"""

JS = r"""
(function(){
const cfg = JSON.parse(document.getElementById('kit-config').textContent);
function flash(btn, text){const old=btn.textContent;btn.textContent=text;btn.classList.add('done');
  setTimeout(()=>{btn.textContent=old;btn.classList.remove('done')},1400);}
async function copy(text, btn){
  try{await navigator.clipboard.writeText(text);}catch(e){
    const t=document.createElement('textarea');t.value=text;document.body.appendChild(t);t.select();
    document.execCommand('copy');t.remove();}
  flash(btn,'Copied');}
document.addEventListener('click', async (ev)=>{
  const b = ev.target.closest('button'); if(!b) return;
  const ed = b.closest('.editor');
  if(b.dataset.choice!==undefined && ed){
    ed.querySelectorAll('.chip').forEach(c=>c.classList.toggle('on', c===b));
    const lib=ed.querySelector('[data-library]');
    try{await post(cfg.answers,{question:ed.dataset.question,answer:b.dataset.choice,save_to_library:lib&&lib.checked});
      ed.querySelector('[data-source]').textContent='saved: select '+b.dataset.choice;}
    catch(e){ed.querySelector('[data-source]').textContent='Not saved: '+e.message;}
    return;}
  if(b.dataset.copy){const el=document.getElementById(b.dataset.copy);
    copy((el.tagName==='TEXTAREA'||el.tagName==='INPUT')?el.value:(el.dataset.value||el.textContent), b); return;}
  if(b.hasAttribute('data-save') && ed){await save(ed, b); return;}
  if(b.hasAttribute('data-redraft') && ed){await draft(ed, true); return;}
  if(b.hasAttribute('data-paste-draft') || b.hasAttribute('data-paste-write')){
    const box=b.parentElement.querySelector('[data-paste]'); const q=box.value.trim(); if(!q) return;
    const ed2=addEditor(q); box.value='';
    if(b.hasAttribute('data-paste-draft')) await draft(ed2, false);
    else ed2.querySelector('textarea').focus();}
});
let n=0;
function addEditor(question){
  const sec=document.getElementById('extra'); sec.hidden=false; n++;
  const wrap=document.createElement('div'); wrap.className='q';
  const id='p'+n;
  wrap.innerHTML='<div class="label"></div><div class="editor"><textarea rows="5" id="'+id+
    '" placeholder="Write your answer"></textarea><div class="meta" data-source></div><div class="actions">'+
    '<button type="button" class="btn small" data-copy="'+id+'">Copy</button>'+
    '<button type="button" class="btn small" data-save>Save</button>'+
    '<label class="lib"><input type="checkbox" data-library> reuse next time</label>'+
    '<button type="button" class="btn small ghost" data-redraft>Redraft</button></div></div>';
  wrap.querySelector('.label').textContent=question;
  const ed=wrap.querySelector('.editor'); ed.dataset.question=question;
  sec.appendChild(wrap); wrap.scrollIntoView({behavior:'smooth',block:'center'}); return ed;}
async function post(url, body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error((data.error&&data.error.message)||('HTTP '+r.status)); return data;}
async function draft(ed, redraft){
  const ta=ed.querySelector('textarea'), meta=ed.querySelector('[data-source]');
  meta.textContent='Drafting from your resume...';
  try{const d=await post(cfg.draft,{question:ed.dataset.question,current:redraft?ta.value:null,redraft:redraft});
    ta.value=d.answer.text; meta.textContent=d.label||'';}
  catch(e){meta.textContent='Could not draft: '+e.message;}}
async function save(ed, btn){
  const ta=ed.querySelector('textarea, input[type=text]'), lib=ed.querySelector('[data-library]');
  try{const d=await post(cfg.answers,{question:ed.dataset.question,answer:ta.value,save_to_library:lib&&lib.checked});
    ed.querySelector('[data-source]').textContent=d.label||'your answer'; flash(btn,'Saved');}
  catch(e){ed.querySelector('[data-source]').textContent='Not saved: '+e.message;}}
document.querySelectorAll('[data-autodraft]').forEach(ed=>draft(ed,false));
document.querySelectorAll('.steps a').forEach(a=>a.addEventListener('click',()=>{
  const d=document.querySelector(a.getAttribute('href')); if(d) d.open=true;}));
})();
"""

__all__: Sequence[str] = ("SOURCE_LABELS", "render")
