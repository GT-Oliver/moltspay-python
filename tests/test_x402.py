"""Tests for x402 protocol client."""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock

import httpx

from moltspay.x402 import X402Client, AsyncX402Client, parse_402_response, PaymentRequired
from moltspay.models import Service
from moltspay.exceptions import PaymentError


@pytest.fixture(autouse=True)
def mock_provider_dns(monkeypatch):
    """Keep protocol tests entirely off real DNS/network."""
    monkeypatch.setattr(
        "moltspay.provider_origin._default_resolver",
        lambda host, port: ["93.184.216.34"],
    )


class TestParse402Response:
    """Test 402 response parsing."""
    
    def test_parse_from_header(self):
        """Parse payment info from X-Payment-Required header."""
        response = Mock()
        response.headers = {
            "X-Payment-Required": json.dumps({
                "amount": "0.99",
                "currency": "USDC",
                "payTo": "0x1234567890abcdef"
            })
        }
        response.json = Mock(return_value={})
        
        result = parse_402_response(response, "test-service")
        
        assert result.amount == 0.99
        assert result.currency == "USDC"
        assert result.pay_to == "0x1234567890abcdef"
        assert result.service_id == "test-service"
    
    def test_parse_from_body(self):
        """Parse payment info from response body."""
        response = Mock()
        response.headers = {}
        response.json = Mock(return_value={
            "amount": 1.49,
            "currency": "USDC",
            "payTo": "0xabcdef1234567890"
        })
        
        result = parse_402_response(response, "image-to-video")
        
        assert result.amount == 1.49
        assert result.pay_to == "0xabcdef1234567890"
    
    def test_parse_invalid_response(self):
        """Raise error on invalid 402 response."""
        response = Mock()
        response.headers = {}
        response.json = Mock(side_effect=Exception("Invalid JSON"))
        
        with pytest.raises(PaymentError):
            parse_402_response(response, "test")


class TestX402Client:
    """Test X402Client."""
    
    def test_discover_services(self):
        """Test service discovery."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_response.json = Mock(return_value={
            "services": [
                {"id": "text-to-video", "name": "Text to Video", "price": 0.99, "currency": "USDC"},
                {"id": "image-to-video", "name": "Image to Video", "price": 1.49, "currency": "USDC"},
            ]
        })
        
        with patch.object(httpx.Client, 'get', return_value=mock_response):
            client = X402Client()
            services = client.discover_services("https://example.com")
            
            assert len(services) == 2
            assert services[0].id == "text-to-video"
            assert services[0].price == 0.99
            assert services[1].id == "image-to-video"
            assert services[1].price == 1.49
    
    def test_call_service_success(self):
        """Test successful service call (no payment needed)."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value={"video_url": "https://example.com/video.mp4"})
        
        with patch.object(httpx.Client, 'post', return_value=mock_response):
            client = X402Client()
            response = client.call_service(
                "https://example.com",
                "text-to-video",
                {"prompt": "a cat"}
            )
            
            assert response.status_code == 200
    
    def test_call_service_402(self):
        """Test service returns 402 Payment Required."""
        mock_response = Mock()
        mock_response.status_code = 402
        mock_response.headers = {
            "X-Payment-Required": json.dumps({
                "amount": "0.99",
                "currency": "USDC",
                "payTo": "0x123"
            })
        }
        
        with patch.object(httpx.Client, 'post', return_value=mock_response):
            client = X402Client()
            response = client.call_service(
                "https://example.com",
                "text-to-video",
                {"prompt": "a cat"}
            )
            
            assert response.status_code == 402
    
    def test_pay_and_call_full_flow(self):
        """Test complete x402 payment flow."""
        # First call returns 402
        response_402 = Mock()
        response_402.status_code = 402
        response_402.headers = {
            "X-Payment-Required": json.dumps({
                "amount": "0.99",
                "currency": "USDC",
                "payTo": "0xprovider123"
            })
        }
        response_402.json = Mock(return_value={})
        
        # Second call (with payment) returns success
        response_200 = Mock()
        response_200.status_code = 200
        response_200.json = Mock(return_value={
            "video_url": "https://example.com/video.mp4",
            "duration": 5
        })
        
        with patch.object(httpx.Client, 'post', side_effect=[response_402, response_200]):
            client = X402Client()
            
            # Mock permit signing function
            def mock_sign_permit(spender, amount):
                return {
                    "owner": "0xagent",
                    "spender": spender,
                    "value": str(int(amount * 1e6)),
                    "deadline": 9999999999,
                    "nonce": 0,
                    "v": 27,
                    "r": "0x123",
                    "s": "0x456"
                }
            
            result = client.pay_and_call(
                "https://example.com",
                "text-to-video",
                {"prompt": "a cat dancing"},
                mock_sign_permit
            )
            
            assert result["video_url"] == "https://example.com/video.mp4"
            assert result["duration"] == 5
    
    def test_pay_and_call_service_error(self):
        """Test handling of service errors."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        
        with patch.object(httpx.Client, 'post', return_value=mock_response):
            client = X402Client()
            
            with pytest.raises(PaymentError) as exc_info:
                client.pay_and_call(
                    "https://example.com",
                    "text-to-video",
                    {"prompt": "test"},
                    lambda s, a: {}
                )
            
            assert "500" in str(exc_info.value)


class TestAsyncX402Client:
    """Test AsyncX402Client."""
    
    @pytest.mark.asyncio
    async def test_discover_services_async(self):
        """Test async service discovery."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_response.json = Mock(return_value={
            "services": [
                {"id": "text-to-video", "price": 0.99, "currency": "USDC"},
            ]
        })
        
        with patch.object(httpx.AsyncClient, 'get', return_value=mock_response):
            client = AsyncX402Client()
            services = await client.discover_services("https://example.com")
            
            assert len(services) == 1
            assert services[0].id == "text-to-video"
