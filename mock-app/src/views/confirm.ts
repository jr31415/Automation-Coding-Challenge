import { formatCents, type Member, type SubAccount } from "../data.js";

export function confirmPage(member: Member, sub: SubAccount, confirmationId: string): string {
  return `
<div class="banner banner-success">Sub-account opened successfully.</div>
<table border="1" cellpadding="6" cellspacing="0" bordercolor="#999">
<tr><td colspan="2" bgcolor="#003366"><font color="white"><b>Confirmation</b></font></td></tr>
<tr><td>Confirmation ID</td><td id="confirmationId">${confirmationId}</td></tr>
<tr><td>Member</td><td>${member.id} (${member.name})</td></tr>
<tr><td>Sub-Account Type</td><td>${sub.type}</td></tr>
<tr><td>Initial Deposit</td><td>${formatCents(sub.balanceCents)}</td></tr>
</table>
<p><a href="/members/${member.id}">Return to member</a></p>
`;
}
