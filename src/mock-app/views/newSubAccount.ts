import type { Member } from "../data.js";

export function newSubAccountPage(
  member: Member,
  errors: string[] | undefined,
  prevValues?: { subAccountType?: string; initialDepositDollars?: string },
): string {
  const errorBanner =
    errors && errors.length > 0
      ? `<div class="banner banner-error">${errors.join("<br>")}</div>`
      : "";
  return `
<h2 style="font-size:16px;">Open New Sub-Account &mdash; Member ${member.id} (${member.name})</h2>
${errorBanner}
<form method="post" action="/members/${member.id}/sub-account/new">
<fieldset>
<legend>Sub-Account Details</legend>
<table class="field-table" border="0" cellpadding="0" cellspacing="0">
<tr>
  <td>Sub-Account Type:</td>
  <td>
    <select name="subAccountType">
      <option value="">-- select --</option>
      <option value="Holiday Club" ${prevValues?.subAccountType === "Holiday Club" ? "selected" : ""}>Holiday Club</option>
      <option value="Vacation Fund" ${prevValues?.subAccountType === "Vacation Fund" ? "selected" : ""}>Vacation Fund</option>
      <option value="Emergency Fund" ${prevValues?.subAccountType === "Emergency Fund" ? "selected" : ""}>Emergency Fund</option>
    </select>
  </td>
</tr>
<tr>
  <td>Initial Deposit ($):</td>
  <td><input type="text" name="initialDepositDollars" value="${prevValues?.initialDepositDollars ?? ""}"></td>
</tr>
<tr>
  <td colspan="2" align="right"><input type="submit" value="Continue"></td>
</tr>
</table>
</fieldset>
</form>
<p><a href="/members/${member.id}">Cancel and return to member</a></p>
`;
}
