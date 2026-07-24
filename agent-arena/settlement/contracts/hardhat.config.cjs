/**
 * 仅用 Hardhat 做编译（solc）。部署走 scripts/deploy.ts（viem，精确控制 legacy 交易，D7）。
 * 用 .cjs 避免 ESM("type":"module") 与 Hardhat TS-config 的兼容问题。
 */
module.exports = {
  solidity: {
    version: "0.8.24",
    settings: {
      optimizer: { enabled: true, runs: 200 },
      evmVersion: "paris", // 保守：避免 PUSH0 等新 opcode 在 Injective EVM 上的兼容问题
    },
  },
  paths: {
    sources: "./contracts",
    artifacts: "./artifacts",
    cache: "./cache",
  },
};
