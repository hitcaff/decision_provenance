// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract ProvenanceRegistry {

    event Anchored(
        address indexed sender,
        bytes32 indexed root,
        string  modelId,
        uint256 recordCount,
        uint256 timestamp
    );

    struct AnchorEntry {
        bytes32 root;
        string  modelId;
        uint256 recordCount;
        uint256 timestamp;
    }

    mapping(address => AnchorEntry[]) public anchors;

    function anchor(
        bytes32 root,
        string calldata modelId,
        uint256 recordCount
    ) external {
        anchors[msg.sender].push(AnchorEntry({
            root:        root,
            modelId:     modelId,
            recordCount: recordCount,
            timestamp:   block.timestamp
        }));

        emit Anchored(msg.sender, root, modelId, recordCount, block.timestamp);
    }

    function getAnchorCount(address sender) external view returns (uint256) {
        return anchors[sender].length;
    }

    function getAnchor(address sender, uint256 index)
        external view
        returns (bytes32 root, string memory modelId, uint256 recordCount, uint256 timestamp)
    {
        AnchorEntry storage e = anchors[sender][index];
        return (e.root, e.modelId, e.recordCount, e.timestamp);
    }
}
