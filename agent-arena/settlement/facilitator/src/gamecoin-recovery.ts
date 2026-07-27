export type MintEventEvidence = {
  transactionHash: `0x${string}`;
  blockNumber: bigint;
  to: string;
  value: bigint;
};

export function findMatchingMintEvidence(
  evidence: readonly MintEventEvidence[],
  expected: {
    transactionHash: `0x${string}`;
    to: string;
    value: bigint;
  },
): bigint | null {
  const expectedHash = expected.transactionHash.toLowerCase();
  const expectedTarget = expected.to.toLowerCase();
  const match = evidence.find(
    (item) =>
      item.transactionHash.toLowerCase() === expectedHash &&
      item.to.toLowerCase() === expectedTarget &&
      item.value === expected.value,
  );
  return match?.blockNumber ?? null;
}
