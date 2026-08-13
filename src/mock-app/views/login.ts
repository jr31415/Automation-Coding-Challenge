export function loginPage(next: string, reason?: string, message?: string): string {
  const banner =
    reason === "timeout"
      ? `<div class="banner banner-error">Your session has expired. Please sign in again.</div>`
      : message
        ? `<div class="banner banner-error">${message}</div>`
        : "";
  return `
${banner}
<table class="field-table" border="0" cellpadding="0" cellspacing="0">
<form method="post" action="/login">
<input type="hidden" name="next" value="${next}">
<tr><td>Username:</td><td><input type="text" name="username"></td></tr>
<tr><td>Password:</td><td><input type="password" name="password"></td></tr>
<tr><td colspan="2" align="right"><input type="submit" value="Sign In"></td></tr>
</form>
</table>
`;
}
