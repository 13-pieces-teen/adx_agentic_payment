// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title ArenaMemorialNFT
 * @notice Arena 402 soulbound memorial NFT.
 *
 *         This contract is for commemorative participation records only.
 *         Tokens are permanently non-transferable and non-tradable. They do
 *         not represent currency, securities, yield, redemption rights, or
 *         any other financial interest.
 *
 *         The implementation is dependency-free and exposes ERC-165,
 *         ERC-721, ERC-721 Metadata, and ERC-5192 interface detection.
 */
contract ArenaMemorialNFT {
    string public constant name = "Arena 402 Memorial";
    string public constant symbol = "arena402";
    uint256 public constant MAX_SUPPLY = 402;

    address public owner;
    string public baseURI;
    uint256 public nextTokenId;

    mapping(uint256 => address) private _owners;
    mapping(address => uint256) private _balances;

    event Transfer(address indexed from, address indexed to, uint256 indexed tokenId);
    event Approval(address indexed owner, address indexed approved, uint256 indexed tokenId);
    event ApprovalForAll(address indexed owner, address indexed operator, bool approved);
    event Locked(uint256 tokenId);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event BaseURIUpdated(string previousBaseURI, string newBaseURI);

    error NotOwner();
    error Soulbound();
    error MintToZero();
    error BalanceQueryForZeroAddress();
    error TokenDoesNotExist();
    error MaxSupplyExceeded();
    error NewOwnerIsZero();

    constructor(string memory initialBaseURI) {
        owner = msg.sender;
        baseURI = initialBaseURI;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    function supportsInterface(bytes4 interfaceId) external pure returns (bool) {
        return interfaceId == 0x01ffc9a7 // ERC-165
            || interfaceId == 0x80ac58cd // ERC-721
            || interfaceId == 0x5b5e139f // ERC-721 Metadata
            || interfaceId == 0xb45a3c0e; // ERC-5192
    }

    function totalSupply() external view returns (uint256) {
        return nextTokenId;
    }

    function balanceOf(address tokenOwner) external view returns (uint256) {
        if (tokenOwner == address(0)) revert BalanceQueryForZeroAddress();
        return _balances[tokenOwner];
    }

    function ownerOf(uint256 tokenId) public view returns (address) {
        address tokenOwner = _owners[tokenId];
        if (tokenOwner == address(0)) revert TokenDoesNotExist();
        return tokenOwner;
    }

    function tokenURI(uint256 tokenId) external view returns (string memory) {
        ownerOf(tokenId);
        return string.concat(baseURI, _toString(tokenId));
    }

    function locked(uint256 tokenId) external view returns (bool) {
        ownerOf(tokenId);
        return true;
    }

    function mint(address to) external onlyOwner returns (uint256 tokenId) {
        tokenId = nextTokenId;
        if (tokenId >= MAX_SUPPLY) revert MaxSupplyExceeded();
        _mint(to, tokenId);
        unchecked {
            nextTokenId = tokenId + 1;
        }
    }

    function mintBatch(address[] calldata recipients)
        external
        onlyOwner
        returns (uint256 firstTokenId)
    {
        uint256 length = recipients.length;
        firstTokenId = nextTokenId;
        if (length > MAX_SUPPLY - firstTokenId) revert MaxSupplyExceeded();

        for (uint256 i = 0; i < length; i++) {
            if (recipients[i] == address(0)) revert MintToZero();
        }
        for (uint256 i = 0; i < length; i++) {
            _mint(recipients[i], firstTokenId + i);
        }
        unchecked {
            nextTokenId = firstTokenId + length;
        }
    }

    function getApproved(uint256 tokenId) external view returns (address) {
        ownerOf(tokenId);
        return address(0);
    }

    function isApprovedForAll(address, address) external pure returns (bool) {
        return false;
    }

    function approve(address, uint256) external pure {
        revert Soulbound();
    }

    function setApprovalForAll(address, bool) external pure {
        revert Soulbound();
    }

    function transferFrom(address, address, uint256) external pure {
        revert Soulbound();
    }

    function safeTransferFrom(address, address, uint256) external pure {
        revert Soulbound();
    }

    function safeTransferFrom(address, address, uint256, bytes calldata) external pure {
        revert Soulbound();
    }

    function setBaseURI(string calldata newBaseURI) external onlyOwner {
        string memory previousBaseURI = baseURI;
        baseURI = newBaseURI;
        emit BaseURIUpdated(previousBaseURI, newBaseURI);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert NewOwnerIsZero();
        address previousOwner = owner;
        owner = newOwner;
        emit OwnershipTransferred(previousOwner, newOwner);
    }

    function _mint(address to, uint256 tokenId) private {
        if (to == address(0)) revert MintToZero();
        _owners[tokenId] = to;
        unchecked {
            _balances[to] += 1;
        }
        emit Transfer(address(0), to, tokenId);
        emit Locked(tokenId);
    }

    function _toString(uint256 value) private pure returns (string memory) {
        if (value == 0) return "0";
        uint256 digits;
        uint256 remaining = value;
        while (remaining != 0) {
            digits++;
            remaining /= 10;
        }
        bytes memory buffer = new bytes(digits);
        while (value != 0) {
            digits -= 1;
            buffer[digits] = bytes1(uint8(48 + value % 10));
            value /= 10;
        }
        return string(buffer);
    }
}
