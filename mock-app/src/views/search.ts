export function searchPage(q: string, notFoundMessage?: string): string {
  const banner = notFoundMessage ? `<div class="banner banner-error">${notFoundMessage}</div>` : "";
  // Legacy-style quirk: this "search" form doesn't call a search endpoint at all --
  // it GETs /members/lookup which 302s straight to /members/:id. No unique id/name
  // on the submit control, table-based layout, no test IDs. Intentionally hostile
  // to naive selector strategies.
  return `
<h2 style="font-size:16px;">Member Search</h2>
${banner}
<table border="0" cellpadding="0" cellspacing="0">
<form method="get" action="/members/lookup">
<tr>
<td>Member ID:</td>
<td><input type="text" name="q" value="${q ?? ""}"></td>
<td><input type="submit" value="Look Up"></td>
</tr>
</form>
</table>
<p style="font-size:12px;color:#555;">Tip: try 12345, 67890, or 11111 (restricted account).</p>
`;
}
