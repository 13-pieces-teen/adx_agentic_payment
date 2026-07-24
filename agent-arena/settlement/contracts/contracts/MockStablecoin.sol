// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title MockStablecoin
 * @notice 测试用稳定币：标准 ERC-20 + EIP-3009 (transferWithAuthorization) + 公开 faucet。
 *         用于 Agent Arena 在 Injective EVM testnet 上跑 x402 结算闭环。
 *         SETTLE-002.5。mock USDC / mock USDT 用同一合约，仅构造参数不同。
 *
 * 设计要点：
 *  - EIP-3009: 买方离线 EIP-712 签名授权转账，facilitator 代付 gas 提交（x402 核心）。
 *  - 防重放 (D4): authorizationState[authorizer][nonce]，用过即锁，重复提交 revert。
 *  - 无外部依赖（不引 OpenZeppelin），单文件自包含，便于审计与部署。
 */
contract MockStablecoin {
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

    // --- EIP-712 / EIP-3009 ---
    // 全部用 keccak256(字符串) 编译期求值，杜绝手写 hex 出错
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

    /// @dev authorizationState[authorizer][nonce] == true 表示该授权已用/已取消
    mapping(address => mapping(bytes32 => bool)) public authorizationState;

    // --- 事件 ---
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce);
    event AuthorizationCanceled(address indexed authorizer, bytes32 indexed nonce);
    event Faucet(address indexed to, uint256 amount);

    // faucet 单次额度（构造时按 decimals 设定）
    uint256 public faucetAmount;

    constructor(string memory _name, string memory _symbol, uint8 _decimals, uint256 _faucetWhole) {
        name = _name;
        symbol = _symbol;
        decimals = _decimals;
        owner = msg.sender;
        faucetAmount = _faucetWhole * (10 ** uint256(_decimals));
        _CACHED_CHAIN_ID = block.chainid;
        _CACHED_DOMAIN_SEPARATOR = _computeDomainSeparator();
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
        require(allowed >= value, "ERC20: insufficient allowance");
        if (allowed != type(uint256).max) {
            allowance[from][msg.sender] = allowed - value;
        }
        _transfer(from, to, value);
        return true;
    }

    function _transfer(address from, address to, uint256 value) internal {
        require(to != address(0), "ERC20: transfer to zero");
        uint256 bal = balanceOf[from];
        require(bal >= value, "ERC20: insufficient balance");
        unchecked {
            balanceOf[from] = bal - value;
            balanceOf[to] += value;
        }
        emit Transfer(from, to, value);
    }

    // ============ 铸币 / faucet ============

    /// @notice owner 任意铸币（发种子资金）
    function mint(address to, uint256 amount) external {
        require(msg.sender == owner, "not owner");
        _mint(to, amount);
    }

    /// @notice 公开领币：任何人可领固定额度（供 expo 现场领币按钮调用）
    function faucet(address to) external {
        _mint(to, faucetAmount);
        emit Faucet(to, faucetAmount);
    }

    function _mint(address to, uint256 amount) internal {
        require(to != address(0), "mint to zero");
        totalSupply += amount;
        unchecked {
            balanceOf[to] += amount;
        }
        emit Transfer(address(0), to, amount);
    }

    // ============ EIP-3009 ============

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

    /**
     * @notice 凭授权转账（x402 结算主路径）。任何人（facilitator）可提交，from 只需离线签名。
     */
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
        require(block.timestamp > validAfter, "auth not yet valid");
        require(block.timestamp < validBefore, "auth expired");
        _useAuthorization(from, nonce);
        bytes32 structHash = keccak256(
            abi.encode(TRANSFER_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce)
        );
        _verifySignature(from, structHash, v, r, s);
        _transfer(from, to, value);
    }

    /**
     * @notice 与 transferWithAuthorization 类似，但要求 msg.sender == to（防抢跑到别的收款人）。
     */
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
        require(to == msg.sender, "caller must be payee");
        require(block.timestamp > validAfter, "auth not yet valid");
        require(block.timestamp < validBefore, "auth expired");
        _useAuthorization(from, nonce);
        bytes32 structHash = keccak256(
            abi.encode(RECEIVE_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce)
        );
        _verifySignature(from, structHash, v, r, s);
        _transfer(from, to, value);
    }

    /// @notice 取消一个尚未使用的授权。
    function cancelAuthorization(address authorizer, bytes32 nonce, uint8 v, bytes32 r, bytes32 s) external {
        require(!authorizationState[authorizer][nonce], "auth already used");
        bytes32 structHash = keccak256(abi.encode(CANCEL_AUTHORIZATION_TYPEHASH, authorizer, nonce));
        _verifySignature(authorizer, structHash, v, r, s);
        authorizationState[authorizer][nonce] = true;
        emit AuthorizationCanceled(authorizer, nonce);
    }

    function _useAuthorization(address authorizer, bytes32 nonce) internal {
        require(!authorizationState[authorizer][nonce], "auth used/canceled");
        authorizationState[authorizer][nonce] = true;
        emit AuthorizationUsed(authorizer, nonce);
    }

    function _verifySignature(address expected, bytes32 structHash, uint8 v, bytes32 r, bytes32 s) internal view {
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", DOMAIN_SEPARATOR(), structHash));
        address recovered = ecrecover(digest, v, r, s);
        require(recovered != address(0) && recovered == expected, "invalid signature");
    }
}
