export interface SubAccount {
  type: string;
  balanceCents: number;
}

export interface Member {
  id: string;
  name: string;
  status: "active" | "restricted";
  savingsBalanceCents: number;
  checkingBalanceCents: number;
  subAccounts: SubAccount[];
}

export const members: Record<string, Member> = {
  "12345": {
    id: "12345",
    name: "Alice Whitfield",
    status: "active",
    savingsBalanceCents: 482_311,
    checkingBalanceCents: 152_004,
    subAccounts: [{ type: "Holiday Club", balanceCents: 10_000 }],
  },
  "67890": {
    id: "67890",
    name: "Marcus Lee",
    status: "active",
    savingsBalanceCents: 9_920,
    checkingBalanceCents: 44_150,
    subAccounts: [],
  },
  "11111": {
    id: "11111",
    name: "Priya Natarajan",
    status: "restricted",
    savingsBalanceCents: 210_500,
    checkingBalanceCents: 0,
    subAccounts: [],
  },
};

export function formatCents(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}
