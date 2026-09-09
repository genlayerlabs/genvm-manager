import pytest

from .leader_public_data import LeaderPublicData, decode, encode


def test_round_trip_from_memoryview() -> None:
	data = LeaderPublicData([b'a', b'bc'])
	assert decode(memoryview(encode(data))) == data


def test_preserves_legacy_encoding() -> None:
	assert LeaderPublicData([b'test']).encode() == b'\xcc\x84test\x86padded'


def test_empty_timeout_decodes_as_no_outputs() -> None:
	assert LeaderPublicData.decode(b'') == LeaderPublicData([])


@pytest.mark.parametrize('encoded', [b'\xc0', b'\xc7\x86padded\x00', b'\xc2\x81\x01'])
def test_rejects_invalid_encoding(encoded: bytes) -> None:
	with pytest.raises(ValueError):
		LeaderPublicData.decode(encoded)
