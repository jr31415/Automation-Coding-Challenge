export function layout(title: string, body: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>${title}</title>
<style>
  body { font-family: "MS Sans Serif", Tahoma, Geneva, sans-serif; background: #d4d0c8; margin: 0; }
  .topbar { background: #003366; color: white; padding: 6px 12px; font-size: 14px; }
  .content { padding: 16px; }
  table { border-collapse: collapse; }
  td, th { padding: 4px 8px; font-size: 13px; }
  .field-table td { padding: 3px 6px; }
  .banner { padding: 8px; margin-bottom: 10px; font-weight: bold; }
  .banner-error { background: #ffdddd; border: 1px solid #cc0000; color: #660000; }
  .banner-success { background: #ddffdd; border: 1px solid #009900; color: #003300; }
  fieldset { border: 1px solid #999; }
</style>
</head>
<body>
<div class="topbar">Riverbend Credit Union &mdash; Staff Admin Console</div>
<div class="content">
${body}
</div>
</body>
</html>`;
}
