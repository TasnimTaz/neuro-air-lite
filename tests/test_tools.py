"""Geocoder tests: source order, HK filtering, fallback and failure mode."""

from __future__ import annotations

import pytest

import domains.hk.tools as tools


class FakeResponse:
    def __init__(self, *, json_data=None, text: str = "", status: int = 200):
        self._json = json_data
        self.text = text
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise tools.httpx.HTTPError(f"status {self.status_code}")


def reset(monkeypatch, sleeps: list[float] | None = None) -> None:
    tools._geocode_cached.cache_clear()
    tools._last_request[0] = 0.0

    def fake_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    monkeypatch.setattr(tools.time, "sleep", fake_sleep)


def test_nominatim_hit_does_not_call_als(monkeypatch):
    reset(monkeypatch)
    calls: list[str] = []

    def fake_get(url, *, params=None, headers=None, timeout=None):
        calls.append(url)
        return FakeResponse(
            json_data=[
                {"lat": "22.4494", "lon": "114.1711", "display_name": "Tai Po, Hong Kong"}
            ]
        )

    monkeypatch.setattr(tools.httpx, "get", fake_get)

    hit = tools.geocode("Tai Po, Hong Kong")

    assert calls == [tools.NOMINATIM_URL]
    assert hit["source"] == "nominatim"
    assert hit["latitude"] == pytest.approx(22.4494)
    assert hit["longitude"] == pytest.approx(114.1711)
    assert hit["score"] is None


def test_outside_hk_is_rejected_and_als_used(monkeypatch):
    reset(monkeypatch)
    calls: list[str] = []
    als_xml = """
    <AddressLookupResult>
      <SuggestedAddress><Address><PremisesAddress><EngPremisesAddress>
        <BuildingName>TAI PO MARKET</BuildingName>
        <GeospatialInformation>
          <Latitude>22.4501</Latitude><Longitude>114.1680</Longitude>
        </GeospatialInformation>
      </EngPremisesAddress></PremisesAddress></Address>
      <ValidationInformation><Score>57.5</Score></ValidationInformation>
      </SuggestedAddress>
    </AddressLookupResult>
    """

    def fake_get(url, *, params=None, headers=None, timeout=None):
        calls.append(url)
        if url == tools.NOMINATIM_URL:
            # e.g. a place name that only exists in London.
            return FakeResponse(json_data=[{"lat": "51.5072", "lon": "-0.1276"}])
        return FakeResponse(text=als_xml)

    monkeypatch.setattr(tools.httpx, "get", fake_get)

    hit = tools.geocode("Tai Po Market")

    assert calls == [tools.NOMINATIM_URL, tools.ALS_URL]
    assert hit["source"] == "als"
    assert hit["label"] == "TAI PO MARKET"
    assert hit["score"] == pytest.approx(57.5)


def test_both_sources_miss_raises_value_error(monkeypatch):
    reset(monkeypatch)

    def fake_get(url, *, params=None, headers=None, timeout=None):
        if url == tools.NOMINATIM_URL:
            return FakeResponse(json_data=[])
        return FakeResponse(text="<AddressLookupResult></AddressLookupResult>")

    monkeypatch.setattr(tools.httpx, "get", fake_get)

    with pytest.raises(ValueError, match="could not geocode"):
        tools.geocode("not a real place at all")


def test_network_error_on_nominatim_falls_back_to_als(monkeypatch):
    reset(monkeypatch)
    als_xml = """
    <AddressLookupResult><SuggestedAddress><Address><PremisesAddress>
      <EngPremisesAddress><BuildingName>TEST</BuildingName>
        <GeospatialInformation><Latitude>22.30</Latitude><Longitude>114.17</Longitude></GeospatialInformation>
      </EngPremisesAddress></PremisesAddress></Address></SuggestedAddress></AddressLookupResult>
    """

    def fake_get(url, *, params=None, headers=None, timeout=None):
        if url == tools.NOMINATIM_URL:
            return FakeResponse(status=503)
        return FakeResponse(text=als_xml)

    monkeypatch.setattr(tools.httpx, "get", fake_get)

    hit = tools.geocode("Test Road")
    assert hit["source"] == "als"


def test_empty_address_is_rejected(monkeypatch):
    reset(monkeypatch)
    with pytest.raises(ValueError, match="non-empty"):
        tools.geocode("   ")


def test_results_are_cached_and_copied(monkeypatch):
    reset(monkeypatch)
    calls: list[str] = []

    def fake_get(url, *, params=None, headers=None, timeout=None):
        calls.append(url)
        return FakeResponse(json_data=[{"lat": "22.4494", "lon": "114.1711"}])

    monkeypatch.setattr(tools.httpx, "get", fake_get)

    first = tools.geocode("Tai Po, Hong Kong")
    second = tools.geocode("Tai Po, Hong Kong")

    assert calls == [tools.NOMINATIM_URL]  # cache hit on the second call
    assert first == second
    assert first is not second

    first["latitude"] = 0.0
    assert tools.geocode("Tai Po, Hong Kong")["latitude"] == pytest.approx(22.4494)


def test_throttle_waits_one_second_between_requests(monkeypatch):
    sleeps: list[float] = []
    reset(monkeypatch, sleeps)
    tools._last_request[0] = tools.time.monotonic()

    tools._throttle()

    assert sleeps and sleeps[-1] == pytest.approx(1.0, abs=0.05)
