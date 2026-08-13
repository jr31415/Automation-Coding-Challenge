import { formatCents, type Member } from "../data.js";

export function memberPage(member: Member): string {
  const subRows = member.subAccounts
    .map(
      (sa) => `<tr><td>${sa.type}</td><td align="right">${formatCents(sa.balanceCents)}</td></tr>`,
    )
    .join("");

  return `
<table width="100%" border="0" cellpadding="0" cellspacing="0">
<tr><td>
  <table border="1" cellpadding="6" cellspacing="0" bordercolor="#999">
    <tr><td colspan="2" bgcolor="#003366"><font color="white"><b>Member ${member.id}</b></font></td></tr>
    <tr><td>Name</td><td>${member.name}</td></tr>
    <tr><td>Status</td><td>${member.status}</td></tr>
    <tr><td>Savings Balance</td><td id="savingsBalance">${formatCents(member.savingsBalanceCents)}</td></tr>
    <tr><td>Checking Balance</td><td>${formatCents(member.checkingBalanceCents)}</td></tr>
  </table>
</td></tr>
<tr><td>&nbsp;</td></tr>
<tr><td>
  <table border="1" cellpadding="6" cellspacing="0" bordercolor="#999">
    <tr><td colspan="2" bgcolor="#003366"><font color="white"><b>Sub-Accounts</b></font></td></tr>
    ${subRows || `<tr><td colspan="2"><i>No sub-accounts on file.</i></td></tr>`}
  </table>
</td></tr>
<tr><td>&nbsp;</td></tr>
<tr><td>
  <a href="/members/${member.id}/sub-account/new" style="display:inline-block;padding:4px 10px;border:1px outset #999;background:#e0e0e0;color:#000;text-decoration:none;font-family:inherit;font-size:13px;">Open New Sub-Account</a>
</td></tr>
</table>
`;
}
