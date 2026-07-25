// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title ArenaMemorial (arena402-m)
 * @notice Arena 402 纪念币 —— soulbound(mint-only)。
 *
 *         红线:除 owner 铸造外,任何账户(含 owner)都不可转出。
 *         没有 transfer / transferFrom / approve 转移路径,也没有 EIP-3009。
 *         纪念币按需无限 mint,铸错地址无需补救(再 mint 给新钱包即可)。
 *
 *         设计与 MockStablecoin 保持同一风格:无外部依赖、单文件自包含、便于审计。
 *         符合 AGENTS.md 红线:绝不发可自由交易的币;不可转性在 _mint 之外
 *         的所有路径上强制成立,DEX 池地址无从建池。
 */
contract ArenaMemorial {
    // --- ERC-20 元数据(只读展示 + 钱包导入用)---
    string public name;
    string public symbol;
    uint8 public immutable decimals;

    // --- 状态 ---
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;

    // --- 权限 ---
    address public owner;

    // --- 事件(保留标准 ERC-20 Transfer,便于浏览器/钱包索引 mint)---
    event Transfer(address indexed from, address indexed to, uint256 value);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    error NotOwner();
    error Soulbound();          // 任何转移尝试统一以此 revert
    error MintToZero();
    error NewOwnerIsZero();

    constructor(string memory _name, string memory _symbol, uint8 _decimals) {
        name = _name;
        symbol = _symbol;
        decimals = _decimals;
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // ============ 铸造(唯一的余额增加路径)============

    /// @notice owner 按需铸造纪念币给指定钱包。无限量、可重复。
    function mint(address to, uint256 amount) external onlyOwner {
        if (to == address(0)) revert MintToZero();
        totalSupply += amount;
        unchecked {
            balanceOf[to] += amount;
        }
        // from = address(0) 表示铸造,符合 ERC-20 事件约定,钱包/浏览器可正确索引
        emit Transfer(address(0), to, amount);
    }

    /// @notice 批量铸造,Task2 批量参赛钱包时省事。
    function mintBatch(address[] calldata recipients, uint256 amount) external onlyOwner {
        uint256 len = recipients.length;
        for (uint256 i = 0; i < len; i++) {
            address to = recipients[i];
            if (to == address(0)) revert MintToZero();
            totalSupply += amount;
            unchecked {
                balanceOf[to] += amount;
            }
            emit Transfer(address(0), to, amount);
        }
    }

    // ============ Soulbound:所有转移路径一律 revert ============
    // 保留标准 ERC-20 函数签名,让钱包/工具识别为 ERC-20,但一律拒绝转移。

    function transfer(address, uint256) external pure returns (bool) {
        revert Soulbound();
    }

    function transferFrom(address, address, uint256) external pure returns (bool) {
        revert Soulbound();
    }

    function approve(address, uint256) external pure returns (bool) {
        revert Soulbound();
    }

    /// @notice 恒为 0:没有授权概念,杜绝任何 allowance 驱动的转移。
    function allowance(address, address) external pure returns (uint256) {
        return 0;
    }

    // ============ owner 管理 ============

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert NewOwnerIsZero();
        address prev = owner;
        owner = newOwner;
        emit OwnershipTransferred(prev, newOwner);
    }
}
