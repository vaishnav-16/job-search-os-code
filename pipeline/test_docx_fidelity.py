"""Round-trip fidelity test for the docx engine — milestone 3, written FIRST (TDD).

The contract under test: the engine copies the template, changes exactly what it
was asked to change, and leaves every other byte of the document alone. The
template's formatting is direct-formatted with inline numPr/ind overrides and
empty spacer paragraphs carrying all vertical rhythm, so "leaves alone" is
asserted at the byte level, not the "looks the same" level.

Run: python pipeline/test_docx_fidelity.py
Renders PDFs with soffice, the engine that writes every shipped resume on
every platform (docx_engine.default_renderer). Override with RESUME_RENDERER.
"""

import html
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

TEMPLATE = HERE.parent / "profile" / "template" / "Vaishnav_Ajai_Resume_Template_Format.docx"

# Matches a whole paragraph. Safe here because the template has no nested
# paragraph containers (no tables, textboxes, or drawings — verified 2026-08-13).
# <w:p\b does not match <w:pPr because 'p' and 'P' are both word characters.
PARA_RE = re.compile(r"<w:p\b[^>]*/>|<w:p\b.*?</w:p>", re.S)

# This whole suite renders the template docx and compares the result byte for
# byte. The template is the owner's own resume format and is not published, so
# in this public copy every leg is skipped. Supply your own template at the
# path above (and a LibreOffice or Word renderer) to run it.
SKIP_REASON = (None if TEMPLATE.exists() else
               "needs the template docx at profile/template/ plus a LibreOffice "
               "or Word renderer; neither is published")

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" — {detail}" if detail else ""))


def read_parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def doc_xml(path: Path) -> str:
    return read_parts(path)["word/document.xml"].decode("utf-8")


def flatten(block: str) -> str:
    """Visible text of a paragraph block. The mandatory space before attributes
    keeps <w:tab .../> and <w:tabs> from matching."""
    text = "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", block, re.S))
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def main() -> int:
    if SKIP_REASON:
        print(f"skip  docx render fidelity ({SKIP_REASON})")
        return 0
    from docx_engine import Doc, render_pdf  # noqa: E402 — the module under test

    tmp = Path(tempfile.mkdtemp(prefix="docx_fidelity_"))
    src = tmp / "in.docx"
    shutil.copy2(TEMPLATE, src)

    parts_before = read_parts(src)
    xml_before = parts_before["word/document.xml"].decode("utf-8")
    blocks_before = PARA_RE.findall(xml_before)

    print("-- template sanity --")
    check("template has 55 paragraphs", len(blocks_before) == 55,
          f"got {len(blocks_before)}")
    check("template already carries a dollar anchor metric",
          re.search(r"\$\d+K\+", xml_before) is not None)
    check("template already carries November 2026",
          "November 2026" in xml_before and "December 2026" not in xml_before)
    check("template slot 1 is Networking OS (Founder), slot 2 SAP override title",
          "Founder" in xml_before and "AI Product Manager" in xml_before
          and "OpenDoor" not in xml_before)

    print("-- paragraph model --")
    doc = Doc.load(src)
    paras = doc.paragraphs()
    check("model sees 55 paragraphs", len(paras) == 55, f"got {len(paras)}")
    bullets = [p for p in paras if p.is_bullet]
    # 16, not 15, since 2026-08-25: decisions-2026-08-24.md §1b ruled the Tuck
    # Research Assistant paragraph becomes a bullet. It was converted IN PLACE
    # (its pPr replaced with paragraph 44's), so the paragraph count above is
    # unchanged at 55 and only the bullet count moves.
    check("finds exactly 16 real bullets (numPr with nonzero numId)",
          len(bullets) == 16, f"got {len(bullets)}: {[p.index for p in bullets]}")
    decoy_ok = all(not p.is_bullet for p in paras
                   if 'w:val="AchievementBullet"' in p.xml and 'w:numId w:val="0"' in p.xml)
    check("numId=0 decoy paragraphs are NOT bullets", decoy_ok)
    spacers = [p for p in paras if p.is_spacer]
    check("spacer paragraphs detected (empty text)", len(spacers) >= 11,
          f"got {len(spacers)}")

    # --- positioning line (decisions-2026-08-24.md §1a, landed 2026-08-25) ---
    # Located by STYLE, never by index: the skill's own rule is to trust find()
    # over indices, and preflight resolves the same paragraph the same way.
    pos = [p for p in paras if 'w:val="Positioning"' in p.xml]
    check("exactly one Positioning paragraph", len(pos) == 1, f"got {len(pos)}")
    if pos:
        # It must sit ABOVE the section rule. Paragraph 3 carries
        # <w:pBdr><w:top w:val="single"/> and is the section-header band that
        # draws the line over WORK EXPERIENCE; paragraph 2 is the only
        # border-free spacer between the contact line and that band. Converting
        # 3 -- which the original plan specified -- would have put the
        # positioning line INSIDE the WORK EXPERIENCE section.
        check("positioning line sits above the section rule",
              pos[0].index < next(p.index for p in paras if "<w:pBdr>" in p.xml),
              f"positioning at {pos[0].index}")
        check("positioning line is NOT a bullet", not pos[0].is_bullet)
        check("positioning line is centered", '<w:jc w:val="center"/>' in pos[0].xml)
        # 40, not 20: the v3 bank clause (signed 2026-09-03) is three whole
        # sentences, 45-62 words. The seed shipped in the template must be a v3
        # clause like any other selection — a short seed would render on one
        # line and pass a check written for v2.
        check("positioning line carries real text (never a [FILL] carrier)",
              len(pos[0].text.split()) >= 40 and "[FILL]" not in pos[0].text,
              f"got {len(pos[0].text.split())} words")

    # §1b: the Tuck Research Assistant entry is a bullet, not a paragraph.
    ra = [p for p in paras if p.text.strip().startswith("Research Assistant")]
    check("Research Assistant entry exists", len(ra) == 1, f"got {len(ra)}")
    if ra:
        check("Research Assistant entry is a bullet (§1b)", ra[0].is_bullet)

    # §1c: the CITIGROUP employer line carries the aggregate range. The city
    # already owns the only right tab stop (11091), so the range is pipe-joined
    # onto it rather than given a tab stop of its own.
    citi = [p for p in paras if p.text.strip().startswith("CITIGROUP")]
    check("CITIGROUP employer line carries the aggregate range (§1c)",
          bool(citi) and "July 2022" in citi[0].text and "June 2025" in citi[0].text,
          citi[0].text.strip() if citi else "not found")

    # --- Para.style / Doc.find_style (added 2026-08-25) ---
    # The positioning line is the one paragraph that CANNOT be located by text:
    # its text is whatever clause pass 1 selected. Both the skill and the live
    # tailor-queue prompt instruct `doc.find_style("Positioning")`, so this API
    # existing is load-bearing for an instruction already shipped.
    found = doc.find_style("Positioning")
    check("find_style locates the positioning paragraph", found.index == pos[0].index,
          f"got {found.index}, expected {pos[0].index}")
    check("Para.style reads a real pStyle off a bullet",
          paras[8].style == "AchievementBullet", repr(paras[8].style))
    check("Para.style is '' when the paragraph carries no pStyle",
          any(p.style == "" for p in paras if p.is_spacer))
    try:
        doc.find_style("NoSuchStyleAnywhere")
        check("find_style raises when a style matches nothing", False, "no exception")
    except ValueError:
        check("find_style raises when a style matches nothing", True)

    # --- page margins (added 2026-09-03) ---
    # Read-only, and asserted against the TEMPLATE because the template is the
    # doctrine: `preflight.check_page_fill` measures the foot of every rendered
    # resume against this number, so a silent change here silently re-grades the
    # whole corpus. 120 twips / 20 = 6.0pt, cut from 244 (12.2pt) on 2026-09-03
    # to buy the v3 positioning line its third rendered line.
    margins = doc.page_margins()
    check("page_margins reads all four sides in points",
          set(margins) == {"top", "right", "bottom", "left"}, str(margins))
    check("the template's bottom margin is 6.0pt (the 2026-09-03 cut)",
          margins.get("bottom") == 6.0, str(margins))
    check("bottom_margin_pt agrees with page_margins",
          doc.bottom_margin_pt() == margins.get("bottom"))

    # --- section headers ---
    # The project block's header is PROJECTS as of 2026-09-03 (it was
    # "LEADERSHIP & OTHER EXPERIENCE"); the skill, the Routine prompt and the
    # bank all name PROJECTS, so the template has to be where that is true.
    header_text = " ".join(p.text for p in paras if not p.is_bullet)
    check("the project section header reads PROJECTS",
          "PROJECTS" in header_text.upper(),
          [p.text for p in paras if p.text.strip().isupper() and p.text.strip()])

    # ------------------------------------------------------------------
    print("-- replace one bullet --")
    target = bullets[0]  # first Fidelity bullet
    NEW_TEXT = ("Owned intake governance & standards for **40 partner integrations**, "
                "cutting review cycle time for <10 platform teams")
    NEW_FLAT = NEW_TEXT.replace("**", "")
    doc.replace_paragraph_text(target.index, NEW_TEXT)
    out = tmp / "out.docx"
    doc.save(out)

    parts_after = read_parts(out)
    check("zip has identical part list in identical order",
          list(parts_before) == list(parts_after))
    dirty = [n for n in parts_before
             if n != "word/document.xml" and parts_before[n] != parts_after[n]]
    check("every part except word/document.xml is byte-identical",
          not dirty, f"changed: {dirty}")

    xml_after = parts_after["word/document.xml"].decode("utf-8")
    blocks_after = PARA_RE.findall(xml_after)
    check("still 55 paragraphs", len(blocks_after) == 55, f"got {len(blocks_after)}")

    diff_idx = [i for i, (a, b) in enumerate(zip(blocks_before, blocks_after)) if a != b]
    check("exactly one paragraph block changed", diff_idx == [target.index],
          f"changed blocks: {diff_idx}")

    # the single-substitution identity: swapping the one edited block into the
    # original must reproduce the output byte-for-byte — this pins namespaces,
    # sectPr, spacers, smallCaps headers, and every other paragraph at once
    rebuilt = xml_before.replace(blocks_before[target.index],
                                 blocks_after[target.index], 1)
    check("document.xml differs ONLY inside the edited paragraph",
          rebuilt == xml_after)

    old_ppr = re.search(r"<w:pPr>.*?</w:pPr>", blocks_before[target.index], re.S)
    new_ppr = re.search(r"<w:pPr>.*?</w:pPr>", blocks_after[target.index], re.S)
    check("edited paragraph's <w:pPr> is byte-identical (pStyle, numPr, tabs, ind, jc)",
          old_ppr and new_ppr and old_ppr.group(0) == new_ppr.group(0))

    flat = flatten(blocks_after[target.index])
    check("new flattened text matches request", flat == NEW_FLAT, f"got {flat!r}")
    check("old bullet text is gone", target.text[:40] not in xml_after)
    bold_run = re.search(
        r"<w:r><w:rPr><w:b/><w:bCs/></w:rPr><w:t[^>]*>40 partner integrations</w:t></w:r>",
        blocks_after[target.index])
    check("**bold** span became a <w:b/><w:bCs/> run", bold_run is not None)
    check("'&' correctly escaped", "&amp; standards" in blocks_after[target.index])
    check("'<' correctly escaped", "&lt;10 platform teams" in blocks_after[target.index])
    try:
        ET.fromstring(xml_after)
        check("output document.xml is well-formed XML", True)
    except ET.ParseError as e:
        check("output document.xml is well-formed XML", False, str(e))
    for ch in "•–’®é":  # • – ’ ® é
        check(f"non-ASCII U+{ord(ch):04X} count preserved",
              xml_before.count(ch) == xml_after.count(ch))
    check("smallCaps count unchanged",
          xml_before.count("<w:smallCaps/>") == xml_after.count("<w:smallCaps/>"))
    check("sectPr unchanged",
          re.search(r"<w:sectPr.*</w:sectPr>", xml_before, re.S).group(0)
          == re.search(r"<w:sectPr.*</w:sectPr>", xml_after, re.S).group(0))

    # ------------------------------------------------------------------
    print("-- replace a single run's text (role-title rename) --")
    doc2 = Doc.load(src)
    # The first role line's RoleChar run, renamed to an invented title.
    role_para = next(p for p in doc2.paragraphs() if 'w:val="RoleChar"' in p.xml)
    OLD_TITLE = html.unescape(re.search(
        r'<w:rStyle w:val="RoleChar"/>(?:(?!</w:r>).)*?<w:t[^>]*>([^<]+)</w:t>',
        role_para.xml, re.S).group(1))
    NEW_TITLE = "Product Owner, Payments"
    doc2.replace_run_text(role_para.index, OLD_TITLE, NEW_TITLE)
    out2 = tmp / "out2.docx"
    doc2.save(out2)
    xml2 = doc_xml(out2)
    blocks2 = PARA_RE.findall(xml2)
    diff2 = [i for i, (a, b) in enumerate(zip(blocks_before, blocks2)) if a != b]
    check("run rename changed exactly one paragraph", diff2 == [role_para.index],
          f"changed: {diff2}")
    new_block2 = blocks2[role_para.index]
    check("RoleChar style preserved on renamed run",
          '<w:rStyle w:val="RoleChar"/>' in new_block2
          and NEW_TITLE in new_block2)
    # Derive the expected date from the template instead of hardcoding it: the
    # invariant under test is "renaming the role leaves the date run alone", not
    # "the date is 2024-2025". A literal here rotted the moment the attested Citi
    # months landed in the template (2026-08-17).
    date_part = flatten(blocks_before[role_para.index]).replace(OLD_TITLE, "").strip()
    check("date run untouched", bool(date_part) and date_part in flatten(new_block2),
          f"expected {date_part!r} in {flatten(new_block2)!r}")
    old_block2 = blocks_before[role_para.index]
    check("rename touched only the <w:t> text",
          old_block2.replace(OLD_TITLE, NEW_TITLE) == new_block2)

    # ------------------------------------------------------------------
    print("-- delete a paragraph --")
    doc3 = Doc.load(src)
    courses = next(p for p in doc3.paragraphs() if p.text.startswith("Courses:"))
    doc3.delete_paragraph(courses.index)
    out3 = tmp / "out3.docx"
    doc3.save(out3)
    xml3 = doc_xml(out3)
    blocks3 = PARA_RE.findall(xml3)
    check("54 paragraphs after delete", len(blocks3) == 54, f"got {len(blocks3)}")
    check("Courses line gone", "Courses:" not in xml3)
    check("delete removed ONLY that block",
          xml_before.replace(blocks_before[courses.index], "", 1) == xml3)

    doc4 = Doc.load(src)
    spacer = next(p for p in doc4.paragraphs() if p.is_spacer)
    try:
        doc4.delete_paragraph(spacer.index)
        check("deleting a spacer paragraph is refused", False, "no exception raised")
    except ValueError:
        check("deleting a spacer paragraph is refused", True)

    # ------------------------------------------------------------------
    print("-- clone_paragraph (the only sanctioned way to ADD a bullet) --")
    PPR = re.compile(r"<w:pPr>.*?</w:pPr>", re.S)
    docC = Doc.load(src)
    first_bullet = next(p for p in docC.paragraphs() if p.is_bullet)
    bi = first_bullet.index
    src_ppr = PPR.search(blocks_before[bi]).group(0)

    new_idx = docC.clone_paragraph(bi, bi + 1)
    check("clone returns the insertion index", new_idx == bi + 1, f"got {new_idx}")
    docC.replace_paragraph_text(new_idx, "Cloned bullet with a **42% metric**")
    outC = tmp / "outC.docx"
    docC.save(outC)
    xmlC = doc_xml(outC)
    blocksC = PARA_RE.findall(xmlC)
    parasC = docC.paragraphs()

    check("56 paragraphs after clone", len(blocksC) == 56, f"got {len(blocksC)}")
    check("clone is a real bullet (numPr survived)", parasC[new_idx].is_bullet)
    check("clone's pPr is byte-identical to its source (pStyle, numPr, tabs, ind, jc)",
          PPR.search(blocksC[new_idx]).group(0) == src_ppr)
    check("source bullet is untouched",
          flatten(blocksC[bi]) == flatten(blocks_before[bi]))
    check("clone carries the new text", "Cloned bullet" in flatten(blocksC[new_idx]))
    check("clone's **bold** span became a real run",
          "<w:b/><w:bCs/>" in blocksC[new_idx])
    check("clone does not duplicate w14:paraId",
          "w14:paraId" not in re.match(r"<w:p\b[^>]*?/?>", blocksC[new_idx]).group(0))
    partsC = read_parts(outC)
    check("every part except word/document.xml is byte-identical after clone",
          all(v == partsC[k] for k, v in parts_before.items()
              if k != "word/document.xml"))
    try:
        ET.fromstring(xmlC)
        check("output document.xml is well-formed XML after clone", True)
    except ET.ParseError as e:
        check("output document.xml is well-formed XML after clone", False, str(e))

    docR = Doc.load(src)
    docR.clone_paragraph(bi, bi + 1)
    docR.delete_paragraph(bi + 1)
    outR = tmp / "outR.docx"
    docR.save(outR)
    check("clone then delete round-trips to the original document.xml",
          doc_xml(outR) == xml_before)

    try:
        Doc.load(src).clone_paragraph(999, 1)
        check("clone rejects an out-of-range source", False, "no exception raised")
    except IndexError:
        check("clone rejects an out-of-range source", True)

    print("-- hyperlink lifecycle: removal prunes the rel, [label](url) adds one --")
    # Simulates the OpenDoor rotate-in: slot 1's Networking OS header (which
    # carries the networking-os.app hyperlink) is replaced by an OpenDoor
    # header with its own link. The old rel must go; the new one must appear.
    doc5 = Doc.load(src)
    hdr = doc5.find("Networking OS")
    doc5.replace_paragraph_text(
        hdr.index,
        "**OpenDoor Justice** | Founding Product Lead | "
        "[opendoorjustice.lovable.app](https://opendoorjustice.lovable.app/)")
    out5 = tmp / "out5.docx"
    doc5.save(out5)
    parts5 = read_parts(out5)
    rels5 = parts5["word/_rels/document.xml.rels"].decode("utf-8")
    xml5 = parts5["word/document.xml"].decode("utf-8")
    check("orphaned hyperlink rel removed on save", "networking-os.app" not in rels5)
    check("new hyperlink rel added as External",
          'Target="https://opendoorjustice.lovable.app/"' in rels5
          and 'TargetMode="External"' in rels5)
    rid_m = re.search(r'<Relationship Id="(rId\d+)"[^>]*opendoorjustice[^>]*/>', rels5)
    check("document.xml references the new rel id",
          rid_m is not None and f'r:id="{rid_m.group(1)}"' in xml5)
    check("link run carries the Hyperlink character style",
          rid_m is not None and
          f'<w:hyperlink r:id="{rid_m.group(1)}" w:history="1"><w:r><w:rPr>'
          '<w:rStyle w:val="Hyperlink"/>' in xml5)
    # The three targets below are the contact rels of the template docx this suite
    # renders. They are placeholders in the public copy: point them at your own
    # template's hyperlink targets when you supply one.
    check("no other rels were touched",
          all(s in rels5 for s in ('Target="https://github.com/vaishnav-16"',
                                   'Target="mailto:you@example.com"',
                                   "linkedin.com/in/example")))
    dirty5 = [n for n in parts_before
              if n not in ("word/document.xml", "word/_rels/document.xml.rels")
              and parts_before[n] != parts5[n]]
    check("only document.xml and its rels changed", not dirty5, f"changed: {dirty5}")
    try:
        ET.fromstring(xml5)
        ET.fromstring(rels5)
        check("document.xml and rels both well-formed", True)
    except ET.ParseError as e:
        check("document.xml and rels both well-formed", False, str(e))

    # ------------------------------------------------------------------
    print("-- spacer resize (overflow ladder step c') --")
    # The template shipped 6pt spacers until 2026-08-25, when the trim to the
    # 4pt floor was baked in as the default to buy the positioning line its
    # second rendered line -- which SPENDS overflow-ladder rung (b). So every
    # template spacer now sits at the floor and the 6pt state this test needs
    # must be BUILT, through the same public API, rather than assumed of the
    # fixture. Asserting the new invariant first is what keeps a silent
    # re-introduction of 6pt spacers from going unnoticed.
    doc6 = Doc.load(src)
    spacers6 = [p for p in doc6.paragraphs() if p.is_spacer]
    check("template has spacers to trim", len(spacers6) > 0, "none found")
    check("every template spacer sits at the 4pt floor (baked in 2026-08-25)",
          all('<w:sz w:val="8"/>' in p.xml for p in spacers6),
          f"not at floor: {[p.index for p in spacers6 if '<w:sz w:val=\"8\"/>' not in p.xml]}")
    sp_index = spacers6[0].index
    doc6.resize_spacer(sp_index, 12)
    fixture6 = tmp / "fixture6.docx"
    doc6.save(fixture6)
    blocks6_before = PARA_RE.findall(doc_xml(fixture6))
    check("built a 6pt spacer fixture", '<w:sz w:val="12"/>' in blocks6_before[sp_index])

    doc6 = Doc.load(fixture6)
    sp = doc6.paragraphs()[sp_index]
    doc6.resize_spacer(sp.index, 8)
    out6 = tmp / "out6.docx"
    doc6.save(out6)
    blocks6 = PARA_RE.findall(doc_xml(out6))
    diff6 = [i for i, (a, b) in enumerate(zip(blocks6_before, blocks6)) if a != b]
    check("resize changed exactly one paragraph", diff6 == [sp.index],
          f"changed: {diff6}")
    check("spacer now 4pt", '<w:sz w:val="8"/>' in blocks6[sp.index]
          and 'w:val="12"' not in blocks6[sp.index])
    try:
        doc6.resize_spacer(sp.index, 6)
        check("floor enforced (never below 4pt)", False, "no exception raised")
    except ValueError:
        check("floor enforced (never below 4pt)", True)
    bullet_p = next(p for p in doc6.paragraphs() if p.is_bullet)
    try:
        doc6.resize_spacer(bullet_p.index, 8)
        check("resize refuses non-spacer paragraphs", False, "no exception raised")
    except ValueError:
        check("resize refuses non-spacer paragraphs", True)

    # ------------------------------------------------------------------
    print("-- move_paragraphs (section reorder) --")
    doc7 = Doc.load(src)
    paras7 = doc7.paragraphs()
    n7 = len(paras7)
    before_blocks = [p.xml for p in paras7]
    before_ppr = sorted(m.group(0) for b in before_blocks
                        for m in [re.search(r"<w:pPr>.*?</w:pPr>", b, re.S)] if m)
    # move a 3-paragraph block from the tail to sit before paragraph 4
    s, e, tgt = n7 - 5, n7 - 3, 4
    moved_texts = [flatten(before_blocks[i]) for i in range(s, e + 1)]
    doc7.move_paragraphs(s, e, tgt)
    out7 = tmp / "moved.docx"
    doc7.save(out7)
    after_blocks = [m.group(0) for m in PARA_RE.finditer(doc_xml(out7))]
    after_ppr = sorted(m.group(0) for b in after_blocks
                       for m in [re.search(r"<w:pPr>.*?</w:pPr>", b, re.S)] if m)
    check("move: paragraph count unchanged", len(after_blocks) == n7,
          f"{n7} -> {len(after_blocks)}")
    check("move: pPr multiset identical (pure permutation)",
          before_ppr == after_ppr)
    check("move: block landed before target",
          [flatten(b) for b in after_blocks[tgt:tgt + 3]] == moved_texts)
    check("move: xml blocks byte-identical as a multiset",
          sorted(before_blocks) == sorted(after_blocks))
    tpl_parts7 = read_parts(src)
    out_parts7 = read_parts(out7)
    check("move: all non-document parts byte-identical",
          all(out_parts7[nm] == tpl_parts7[nm] for nm in tpl_parts7
              if nm != "word/document.xml"))
    doc7b = Doc.load(src)
    try:
        doc7b.move_paragraphs(2, 5, 3)
        check("move: refuses target inside block", False, "no exception raised")
    except ValueError:
        check("move: refuses target inside block", True)

    # ------------------------------------------------------------------
    print("-- a rewrite keeps the template's bold label (2026-10-08) --")

    def bold_runs(d, idx):
        block = d.paragraphs()[idx].xml
        return [flatten(r) for r in re.findall(r"<w:r(?: [^>]*)?>.*?</w:r>", block, re.S)
                if re.search(r"<w:b/>", r) and flatten(r).strip()]
    d8 = Doc.load(src)
    tech = d8.find("Python, Java, SQL").index
    d8.replace_paragraph_text(tech, "Technical: SQL, Python, Kafka")
    check("skills label stays bold, list stays plain", bold_runs(d8, tech) == ["Technical:"],
          f"got {bold_runs(d8, tech)}")
    courses = d8.find("Courses").index
    d8.replace_paragraph_text(courses, "Courses: Platform Strategy, Applied Machine Learning")
    check("Courses keeps the template's bold word", bold_runs(d8, courses) == ["Courses"],
          f"got {bold_runs(d8, courses)}")
    sap = d8.find("SAP Experience Garage").index
    d8.replace_paragraph_text(sap, "SAP Experience Garage | AI Product Manager")
    check("same project name stays bold, role plain",
          bold_runs(d8, sap) == ["SAP Experience Garage"], f"got {bold_runs(d8, sap)}")
    clone = d8.clone_paragraph(sap, sap)
    d8.replace_paragraph_text(clone, "Heat Wise | Project Manager")
    check("a new project name on a cloned header is bold",
          bold_runs(d8, clone) == ["Heat Wise"], f"got {bold_runs(d8, clone)}")
    nos = d8.find("Networking OS").index
    d8.replace_paragraph_text(nos, "OpenDoor Justice | Founder | [opendoor.app](https://opendoor.app)")
    check("a linked header bolds only the name",
          bold_runs(d8, nos) == ["OpenDoor Justice"], f"got {bold_runs(d8, nos)}")
    bullet = d8.find("Drove a firm-mandated").index
    d8.replace_paragraph_text(bullet, "Moved all 140 applications off a legacy API")
    check("a bullet stays plain", bold_runs(d8, bullet) == [], f"got {bold_runs(d8, bullet)}")
    d8.replace_paragraph_text(tech, "**Tools:** SQL")
    check("explicit ** markers still win", bold_runs(d8, tech) == ["Tools:"],
          f"got {bold_runs(d8, tech)}")

    # ------------------------------------------------------------------
    print("-- delete_employer: Penzigo is opt-in (2026-10-13) --")
    d9 = Doc.load(src)
    before = [p.text.strip() for p in d9.paragraphs()]
    edu = before.index("EDUCATION")
    tail_before = [p.is_spacer for p in d9.paragraphs()][edu - 2:edu]
    removed = d9.delete_employer("PENZIGO")
    after = [p.text.strip() for p in d9.paragraphs()]
    check("the Penzigo block is gone", not any(t.upper().startswith("PENZIGO") for t in after)
          and not any("2021" in t for t in after[:after.index("EDUCATION")]), str(after[:30]))
    check("it took the spacer above, the header, role and bullet", removed == 4, str(removed))
    e2 = after.index("EDUCATION")
    check("the two end-of-section spacers before EDUCATION stay",
          [p.is_spacer for p in d9.paragraphs()][e2 - 2:e2] == tail_before == [True, True])
    check("the last Citi bullet now sits right above them",
          d9.paragraphs()[e2 - 3].is_bullet, after[e2 - 3])
    for keep in ("CITIGROUP", "FIDELITY"):
        try:
            Doc.load(src).delete_employer(keep)
            check(f"{keep} cannot be deleted", False)
        except ValueError:
            check(f"{keep} cannot be deleted", True)
    no_penzigo = tmp / "no_penzigo.docx"
    d9.save(no_penzigo)

    # ------------------------------------------------------------------
    # Whichever renderer this machine has. Word locally (ground truth), soffice
    # in the cloud Routine. The one-page assertion is the point and it holds for
    # both; compare_renderers.py is what proves they still agree on the count.
    from docx_engine import default_renderer
    engine = default_renderer()
    print(f"-- PDF page count via {engine} --")
    _, pages_in = render_pdf(src)
    check("template renders to exactly 1 page", pages_in == 1, f"got {pages_in}")
    _, pages_out = render_pdf(out)
    check("edited docx renders to exactly 1 page", pages_out == 1, f"got {pages_out}")
    _, pages_np = render_pdf(no_penzigo)
    check("a page without Penzigo renders to 1 page", pages_np == 1, f"got {pages_np}")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
