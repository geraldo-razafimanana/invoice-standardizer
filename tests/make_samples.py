"""Builds the sample pack: original PDFs, extracted JSON, standardized PDFs, and a summary."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import extraction
from app.checks import validate
from app.parser import parse_document
from app.pdfgen import build_pdf, standardized_filename
from tests import sample_invoices as S

root = Path(__file__).resolve().parents[2] / "samples"
cases = {
    "01_classique_anglais_USD": S.classic_en()[0],
    "02_facture_francaise_EUR_TVA_multiple": S.french_ttc()[0],
    "03_facture_madagascar_Ariary": S.mga_madagascar()[0],
    "04_remises_et_taxes_GBP": S.discounts_multi_tax()[0],
    "05_informations_manquantes": S.missing_info()[0],
    "06_montants_incoherents": S.inconsistent()[0],
    "07_mise_en_page_differente_AUD": S.different_layout()[0],
    "08_multipage_55_lignes": S.multipage(55)[0],
    "09_scan_remises_et_taxes_GBP": S.to_scanned(S.discounts_multi_tax()[0], noise=True),
    "10_scan_classique_USD": S.to_scanned(S.classic_en()[0], noise=True),
}
md = ["# Résumé des exemples\n"]
for name, pdf in cases.items():
    (root / "originals" / f"{name}.pdf").write_bytes(pdf)
    doc = extraction.open_pdf(pdf)
    text = extraction.extract_words(doc)
    out = parse_document(text)
    d = out.data
    warns = [w.message for w in validate(d) if w.severity != "info"]
    (root / "extracted" / f"{name}.json").write_text(json.dumps(
        {"methode": text.method, "donnees": d.model_dump(), "champs_a_verifier": out.flags, "avertissements": warns},
        ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "standardized" / f"{name}__{standardized_filename(d)}").write_bytes(
        build_pdf(d, original_filename=f"{name}.pdf", original_pages=doc.page_count))
    md.append(f"## {name}\n- Lecture : **{text.method}** · {doc.page_count} page(s)\n"
              f"- N° facture : `{d.invoice_number or '—'}` · Date : `{d.issue_date or '—'}` · Échéance : `{d.due_date or '—'}` · Réf/BC : `{d.po_number or '—'}`\n"
              f"- Fournisseur : {d.supplier.name or '—'} · Client : {d.customer.name or '—'}\n"
              f"- Lignes : {len(d.items)} · Sous-total : `{d.subtotal or '—'}` · Taxes : {', '.join((t.rate+'% → ' if t.rate else '')+t.amount for t in d.taxes) or '—'} · **Total : `{d.total or '—'}`** · Devise : `{d.currency or '—'}`\n"
              f"- À vérifier : {', '.join(out.flags) or 'rien'}\n"
              f"- Avertissements : {'; '.join(warns) or 'aucun'}\n")
(root / "RESUME.md").write_text("\n".join(md), encoding="utf-8")
print("ok")
