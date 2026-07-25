// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title ArenaGameCoin (arena402-g)
 * @notice Arena 402 游戏币 —— 白名单受限转账的 ERC-20 + EIP-3009。
 *
 *         红线(见 AGENTS.md):绝不发可自由交易的币。
 *         _transfer 要求 from 与 to 都在白名单内 → 只有登记过的参赛钱包之间能转。
 *         DEX 池合约地址不会被登记 → 建不了池 → 无法在外部市场炒作。
 *         白名单检查放在 _transfer,覆盖 transfer / transferFrom / EIP-3009 全部路径。
 *
 *         结算路径与 MockStablecoin 一致:买方离线 EIP-3009 签名,facilitator 代付
 *         gas 提交 transferWithAuthorization。facilitator 本身无需在白名单(它不是
 *         from/to,只是 msg.sender)。
 *
 *         mint 例外:铸造(from=address(0))与销毁不受白名单约束,便于 owner 分发。
 */
contract ArenaGameCoin {
    // --- ERC-20 元数据 ---
    string public name;
    string public symbol;
    uint8 public immutable decimals;

    // --- ERC-20 状态 ---
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    // --- 权限 ---
    address public owner;

    // --- 白名单:只有登记的参赛钱包之间可转 ---
    mapping(address => bool) public whitelisted;

    // --- EIP-712 / EIP-3009(与 MockStablecoin 同一套,保证 SDK 复用)---
    bytes32 public constant EIP712_VERSION_HASH = keccak256(bytes("1"));
    bytes32 private constant _DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 public constant TRANSFER_WITH_AUTHORIZATION_TYPEHASH =
        keccak256("TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)");
    bytes32 public constant RECEIVE_WITH_AUTHORIZATION_TYPEHASH =
        keccak256("ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)");
    bytes32 public constant CANCEL_AUTHORIZATION_TYPEHASH =
        keccak256("CancelAuthorization(address authorizer,bytes32 nonce)");

    bytes32 private immutable _CACHED_DOMAIN_SEPARATOR;
    uint256 private immutable _CACHED_CHAIN_ID;

    mapping(address => mapping(bytes32 => bool)) public authorizationState;

    // --- 事件 ---
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce);
    event AuthorizationCanceled(address indexed authorizer, bytes32 indexed nonce);
    event Whitelisted(address indexed account, bool allowed);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    error NotOwner();
    error NotWhitelisted(address account);
    error TransferToZero();
    error InsufficientBalance();
    error InsufficientAllowance();
    error MintToZero();
    error AuthNotYetValid();
    error AuthExpired();
    error AuthUsed();
    error InvalidSignature();
    error CallerMustBePayee();
    error NewOwnerIsZero();

    constructor(string memory _name, string memory _symbol, uint8 _decimals) {
        name = _name;
        symbol = _symbol;
        decimals = _decimals;
        owner = msg.sender;
        _CACHED_CHAIN_ID = block.chainid;
        _CACHED_DOMAIN_SEPARATOR = _computeDomainSeparator();
        emit OwnershipTransferred(address(0), msg.sender);
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // ============ 白名单管理 ============

    function addToWhitelist(address account) external onlyOwner {
        whitelisted[account] = true;
        emit Whitelisted(account, true);
    }

    function addToWhitelistBatch(address[] calldata accounts) external onlyOwner {
        uint256 len = accounts.length;
        for (uint256 i = 0; i < len; i++) {
            whitelisted[accounts[i]] = true;
            emit Whitelisted(accounts[i], true);
        }
    }

    function removeFromWhitelist(address account) external onlyOwner {
        whitelisted[account] = false;
        emit Whitelisted(account, false);
    }

    // ============ ERC-20 ============

    function transfer(address to, uint256 value) external returns (bool) {
        _transfer(msg.sender, to, value);
        return true;
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed < value) revert InsufficientAllowance();
        if (allowed != type(uint256).max) {
            allowance[from][msg.sender] = allowed - value;
        }
        _transfer(from, to, value);
        return true;
    }

    /// @dev 唯一的转移实现,白名单红线在此强制,覆盖所有转账路径(含 EIP-3009)。
    function _transfer(address from, address to, uint256 value) internal {
        if (to == address(0)) revert TransferToZero();
        // 红线:转账双方都必须是登记过的参赛钱包。
        if (!whitelisted[from]) revert NotWhitelisted(from);
        if (!whitelisted[to]) revert NotWhitelisted(to);
        uint256 bal = balanceOf[from];
        if (bal < value) revert InsufficientBalance();
        unchecked {
            balanceOf[from] = bal - value;
            balanceOf[to] += value;
        }
        emit Transfer(from, to, value);
    }

    // ============ 铸造(不受白名单约束,owner 分发用)============

    function mint(address to, uint256 amount) external onlyOwner {
        _mint(to, amount);
    }

    function mintBatch(address[] calldata recipients, uint256 amount) external onlyOwner {
        uint256 len = recipients.length;
        for (uint256 i = 0; i < len; i++) {
            _mint(recipients[i], amount);
        }
    }

    function _mint(address to, uint256 amount) internal {
        if (to == address(0)) revert MintToZero();
        totalSupply += amount;
        unchecked {
            balanceOf[to] += amount;
        }
        emit Transfer(address(0), to, amount);
    }

    // ============ EIP-3009(与 MockStablecoin 同实现,白名单经 _transfer 生效)============

    function DOMAIN_SEPARATOR() public view returns (bytes32) {
        if (block.chainid == _CACHED_CHAIN_ID) return _CACHED_DOMAIN_SEPARATOR;
        return _computeDomainSeparator();
    }

    function _computeDomainSeparator() internal view returns (bytes32) {
        return keccak256(
            abi.encode(
                _DOMAIN_TYPEHASH,
                keccak256(bytes(name)),
                EIP712_VERSION_HASH,
                block.chainid,
                address(this)
            )
        );
    }

    function transferWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external {
        if (block.timestamp <= validAfter) revert AuthNotYetValid();
        if (block.timestamp >= validBefore) revert AuthExpired();
        _useAuthorization(from, nonce);
        bytes32 structHash = keccak256(
            abi.encode(TRANSFER_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce)
        );
        _verifySignature(from, structHash, v, r, s);
        _transfer(from, to, value);
    }

    function receiveWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external {
        if (to != msg.sender) revert CallerMustBePayee();
        if (block.timestamp <= validAfter) revert AuthNotYetValid();
        if (block.timestamp >= validBefore) revert AuthExpired();
        _useAuthorization(from, nonce);
        bytes32 structHash = keccak256(
            abi.encode(RECEIVE_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce)
        );
        _verifySignature(from, structHash, v, r, s);
        _transfer(from, to, value);
    }

    function cancelAuthorization(address authorizer, bytes32 nonce, uint8 v, bytes32 r, bytes32 s) external {
        if (authorizationState[authorizer][nonce]) revert AuthUsed();
        bytes32 structHash = keccak256(abi.encode(CANCEL_AUTHORIZATION_TYPEHASH, authorizer, nonce));
        _verifySignature(authorizer, structHash, v, r, s);
        authorizationState[authorizer][nonce] = true;
        emit AuthorizationCanceled(authorizer, nonce);
    }

    function _useAuthorization(address authorizer, bytes32 nonce) internal {
        if (authorizationState[authorizer][nonce]) revert AuthUsed();
        authorizationState[authorizer][nonce] = true;
        emit AuthorizationUsed(authorizer, nonce);
    }

    function _verifySignature(address expected, bytes32 structHash, uint8 v, bytes32 r, bytes32 s) internal view {
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", DOMAIN_SEPARATOR(), structHash));
        address recovered = ecrecover(digest, v, r, s);
        if (recovered == address(0) || recovered != expected) revert InvalidSignature();
    }

    // ============ owner 管理 ============

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert NewOwnerIsZero();
        address prev = owner;
        owner = newOwner;
        emit OwnershipTransferred(prev, newOwner);
    }
}
