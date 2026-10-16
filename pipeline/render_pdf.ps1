# Render a .docx to PDF via Word COM and report the page count.
#
# Usage: powershell -NoProfile -File pipeline/render_pdf.ps1 <in.docx> <out.pdf>
# Prints "PAGES=<n>" on success; exits nonzero on failure.
#
# What this is FOR (revised 2026-08-19): the page-count cross-check only. Word's
# pagination is what a recruiter sees, so a disagreement with soffice means the
# resume is not ready. It must NOT write a PDF anyone submits -- Word's PDF text
# layer sprays spaces inside compound keywords ("real -time", "cross -functional")
# and an ATS parses that text layer. soffice writes every shipped PDF; callers
# render this to scratch. See docx_engine.render_pdf.
#
# (The older note here claimed LibreOffice was not installed. It is: 26.2.5.2,
# at C:\Program Files\LibreOffice\program\soffice.exe.)
#
# The template carries DocSecurity=2 (read-only recommended) in app.xml, so the
# document is always opened ReadOnly to suppress the prompt.

param(
    [Parameter(Mandatory = $true)][string]$DocxPath,
    [Parameter(Mandatory = $true)][string]$PdfPath
)

$ErrorActionPreference = "Stop"

$docx = (Resolve-Path $DocxPath).Path
# Resolve-Path fails on not-yet-existing files; build the PDF path by hand.
$pdf = [System.IO.Path]::GetFullPath($PdfPath)

$word = $null
$doc = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0    # wdAlertsNone

    # Open(FileName, ConfirmConversions:=$false, ReadOnly:=$true)
    $doc = $word.Documents.Open($docx, $false, $true)

    # 17 = wdExportFormatPDF
    $doc.ExportAsFixedFormat($pdf, 17)

    # 2 = wdStatisticPages (forces repagination, so this is the rendered count)
    $pages = $doc.ComputeStatistics(2)
    Write-Output "PAGES=$pages"
}
finally {
    if ($doc) { $doc.Close(0) | Out-Null }     # 0 = wdDoNotSaveChanges
    if ($word) { $word.Quit() | Out-Null }
    foreach ($o in @($doc, $word)) {
        if ($o) { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($o) }
    }
    # ReleaseComObject alone does NOT reliably end WINWORD.EXE: the runtime
    # callable wrappers survive until they are collected, and until then the
    # headless Word process keeps an exclusive handle on the .docx. Measured
    # 2026-08-20 -- a leaked instance (no window, no title, started at the last
    # cross-check) made the NEXT `doc.save()` fail with PermissionError, which
    # reads exactly like the user having the file open and is not that. Drop the
    # references and force a full collect so the process actually exits.
    $doc = $null
    $word = $null
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()
}
