"""Tests for MoltsPay client - complete flow tests."""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

import httpx

from moltspay import MoltsPay, AsyncMoltsPay
from moltspay.models import Service, PaymentResult
from moltspay.exceptions import PaymentError, LimitExceeded, InsufficientFunds


@pytest.fixture(autouse=True)
def mock_provider_dns(monkeypatch):
    """Provider HTTP is mocked; do not consult the host resolver in tests."""
    monkeypatch.setattr(
        "moltspay.provider_origin._default_resolver",
        lambda host, port: ["93.184.216.34"],
    )


class TestMoltsPayInit:
    """Test MoltsPay initialization."""
    
    def test_init_creates_wallet(self, tmp_path):
        """Test that MoltsPay creates wallet on init."""
        wallet_path = tmp_path / "wallet.json"
        
        client = MoltsPay(wallet_path=str(wallet_path))
        
        assert client.address.startswith("0x")
        assert len(client.address) == 42
        assert wallet_path.exists()
    
    def test_init_loads_existing_wallet(self, tmp_path):
        """Test that MoltsPay loads existing wallet."""
        wallet_path = tmp_path / "wallet.json"
        
        # Create first
        client1 = MoltsPay(wallet_path=str(wallet_path))
        address1 = client1.address
        
        # Load second time
        client2 = MoltsPay(wallet_path=str(wallet_path))
        
        assert client2.address == address1
    
    def test_init_with_private_key(self):
        """Test init with explicit private key."""
        test_key = "0x" + "a" * 64
        
        client = MoltsPay(private_key=test_key)
        
        assert client.address.startswith("0x")


class TestMoltsPayDiscovery:
    """Test service discovery."""
    
    def test_discover_services(self, tmp_path):
        """Test discovering services from a provider."""
        wallet_path = tmp_path / "wallet.json"
        
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_response.json = Mock(return_value={
            "services": [
                {
                    "id": "text-to-video",
                    "name": "Text to Video",
                    "description": "Generate video from text prompt",
                    "price": 0.99,
                    "currency": "USDC"
                },
                {
                    "id": "image-to-video",
                    "name": "Image to Video", 
                    "description": "Animate an image",
                    "price": 1.49,
                    "currency": "USDC"
                }
            ]
        })
        
        with patch.object(httpx.Client, 'get', return_value=mock_response):
            client = MoltsPay(wallet_path=str(wallet_path))
            services = client.discover("https://juai8.com/zen7")
            
            assert len(services) == 2
            assert services[0].id == "text-to-video"
            assert services[0].price == 0.99
            assert services[0].name == "Text to Video"
            assert services[1].id == "image-to-video"
            assert services[1].price == 1.49


class TestMoltsPayLimits:
    """Test spending limits."""
    
    def test_default_limits(self, tmp_path):
        """Test default spending limits."""
        wallet_path = tmp_path / "wallet.json"
        client = MoltsPay(wallet_path=str(wallet_path))
        
        limits = client.limits()
        
        assert limits.max_per_tx == 10
        assert limits.max_per_day == 100
        assert limits.spent_today == 0
    
    def test_set_limits(self, tmp_path):
        """Test setting spending limits."""
        wallet_path = tmp_path / "wallet.json"
        client = MoltsPay(wallet_path=str(wallet_path))
        
        client.set_limits(max_per_tx=50, max_per_day=500)
        limits = client.limits()
        
        assert limits.max_per_tx == 50
        assert limits.max_per_day == 500
    
    def test_limits_persist(self, tmp_path):
        """Test that limits persist across instances."""
        wallet_path = tmp_path / "wallet.json"
        
        # Set limits
        client1 = MoltsPay(wallet_path=str(wallet_path))
        client1.set_limits(max_per_tx=25, max_per_day=250)
        
        # Load again
        client2 = MoltsPay(wallet_path=str(wallet_path))
        limits = client2.limits()
        
        assert limits.max_per_tx == 25
        assert limits.max_per_day == 250


class TestMoltsPayPayment:
    """Test payment flow."""
    
    def test_pay_success(self, tmp_path):
        """Test successful payment flow."""
        wallet_path = tmp_path / "wallet.json"
        
        # Mock discover response
        discover_response = Mock()
        discover_response.status_code = 200
        discover_response.raise_for_status = Mock()
        discover_response.json = Mock(return_value={
            "services": [{"id": "text-to-video", "price": 0.99, "currency": "USDC"}]
        })
        
        # Mock 402 response
        response_402 = Mock()
        response_402.status_code = 402
        response_402.headers = {"X-Payment-Required": json.dumps({
            "amount": "0.99", "currency": "USDC", "payTo": "0x1234567890123456789012345678901234567890"
        })}
        response_402.json = Mock(return_value={})
        
        # Mock success response
        response_200 = Mock()
        response_200.status_code = 200
        response_200.json = Mock(return_value={
            "video_url": "https://example.com/video.mp4",
            "duration": 5
        })
        
        with patch.object(httpx.Client, 'get', return_value=discover_response):
            with patch.object(httpx.Client, 'post', side_effect=[response_402, response_200]):
                client = MoltsPay(wallet_path=str(wallet_path))
                
                result = client.pay(
                    "https://juai8.com/zen7",
                    "text-to-video",
                    prompt="a cat dancing"
                )
                
                assert result.success
                assert result.amount == 0.99
                assert result.service_id == "text-to-video"
                assert result.result["video_url"] == "https://example.com/video.mp4"
    
    def test_pay_exceeds_per_tx_limit(self, tmp_path):
        """Test payment rejected when exceeding per-tx limit."""
        wallet_path = tmp_path / "wallet.json"
        
        # Mock discover response with expensive service
        discover_response = Mock()
        discover_response.status_code = 200
        discover_response.raise_for_status = Mock()
        discover_response.json = Mock(return_value={
            "services": [{"id": "expensive-service", "price": 50.0, "currency": "USDC"}]
        })
        
        with patch.object(httpx.Client, 'get', return_value=discover_response):
            client = MoltsPay(wallet_path=str(wallet_path))
            client.set_limits(max_per_tx=10)  # Limit is 10, service costs 50
            
            with pytest.raises(LimitExceeded) as exc_info:
                client.pay("https://example.com", "expensive-service")
            
            assert exc_info.value.limit_type == "per_tx"
            assert exc_info.value.limit == 10
            assert exc_info.value.amount == 50.0
    
    def test_pay_exceeds_daily_limit(self, tmp_path):
        """Test payment rejected when exceeding daily limit."""
        wallet_path = tmp_path / "wallet.json"
        
        # Mock discover response
        discover_response = Mock()
        discover_response.status_code = 200
        discover_response.raise_for_status = Mock()
        discover_response.json = Mock(return_value={
            "services": [{"id": "service", "price": 5.0, "currency": "USDC"}]
        })
        
        with patch.object(httpx.Client, 'get', return_value=discover_response):
            client = MoltsPay(wallet_path=str(wallet_path))
            client.set_limits(max_per_tx=10, max_per_day=20)
            
            # Simulate already spent 18 today
            client._wallet._spent_today = 18
            
            with pytest.raises(LimitExceeded) as exc_info:
                client.pay("https://example.com", "service")  # Would be 18 + 5 = 23 > 20
            
            assert exc_info.value.limit_type == "daily"
    
    def test_pay_service_not_found(self, tmp_path):
        """Test payment fails when service not found."""
        wallet_path = tmp_path / "wallet.json"
        
        discover_response = Mock()
        discover_response.status_code = 200
        discover_response.raise_for_status = Mock()
        discover_response.json = Mock(return_value={"services": []})
        
        with patch.object(httpx.Client, 'get', return_value=discover_response):
            client = MoltsPay(wallet_path=str(wallet_path))
            
            with pytest.raises(PaymentError) as exc_info:
                client.pay("https://example.com", "nonexistent-service")
            
            assert "not found" in str(exc_info.value).lower()
    
    def test_spending_tracked_after_payment(self, tmp_path):
        """Test that spending is tracked after successful payment."""
        wallet_path = tmp_path / "wallet.json"
        
        # Mock discover
        discover_response = Mock()
        discover_response.status_code = 200
        discover_response.raise_for_status = Mock()
        discover_response.json = Mock(return_value={
            "services": [{"id": "service", "price": 2.50, "currency": "USDC"}]
        })
        
        # Mock payment flow
        response_402 = Mock()
        response_402.status_code = 402
        response_402.headers = {"X-Payment-Required": json.dumps({
            "amount": "2.50", "currency": "USDC", "payTo": "0x1234567890123456789012345678901234567890"
        })}
        response_402.json = Mock(return_value={})
        
        response_200 = Mock()
        response_200.status_code = 200
        response_200.json = Mock(return_value={"result": "ok"})
        
        with patch.object(httpx.Client, 'get', return_value=discover_response):
            with patch.object(httpx.Client, 'post', side_effect=[response_402, response_200]):
                client = MoltsPay(wallet_path=str(wallet_path))
                
                # Initial spending
                assert client.limits().spent_today == 0
                
                # Make payment
                client.pay("https://example.com", "service")
                
                # Spending should be tracked
                assert client.limits().spent_today == 2.50


class TestMoltsPayFullFlow:
    """Test complete discovery → select → pay flow."""
    
    def test_complete_flow(self, tmp_path):
        """
        Complete user flow:
        1. Initialize client
        2. Discover available services
        3. Select a service based on criteria
        4. Pay for the service
        5. Receive and use result
        """
        wallet_path = tmp_path / "wallet.json"
        
        # Mock discover response - provider offers multiple services
        discover_response = Mock()
        discover_response.status_code = 200
        discover_response.raise_for_status = Mock()
        discover_response.json = Mock(return_value={
            "services": [
                {
                    "id": "text-to-video",
                    "name": "Text to Video",
                    "description": "Generate video from text",
                    "price": 0.99,
                    "currency": "USDC",
                    "parameters": {"prompt": {"type": "string", "required": True}}
                },
                {
                    "id": "image-to-video",
                    "name": "Image to Video",
                    "description": "Animate an image",
                    "price": 1.49,
                    "currency": "USDC",
                    "parameters": {
                        "image_url": {"type": "string", "required": True},
                        "prompt": {"type": "string", "required": False}
                    }
                },
                {
                    "id": "video-upscale",
                    "name": "Video Upscale",
                    "description": "Upscale video to 4K",
                    "price": 2.99,
                    "currency": "USDC"
                }
            ]
        })
        
        # Mock payment flow
        response_402 = Mock()
        response_402.status_code = 402
        response_402.headers = {"X-Payment-Required": json.dumps({
            "amount": "0.99", "currency": "USDC", "payTo": "0x1234567890123456789012345678901234567890"
        })}
        response_402.json = Mock(return_value={})
        
        response_200 = Mock()
        response_200.status_code = 200
        response_200.json = Mock(return_value={
            "video_url": "https://cdn.zen7.com/videos/abc123.mp4",
            "duration": 5,
            "resolution": "1080p",
            "status": "completed"
        })
        
        with patch.object(httpx.Client, 'get', return_value=discover_response):
            with patch.object(httpx.Client, 'post', side_effect=[response_402, response_200]):
                
                # Step 1: Initialize client
                client = MoltsPay(wallet_path=str(wallet_path))
                print(f"✓ Wallet created: {client.address}")
                
                # Set reasonable limits
                client.set_limits(max_per_tx=5, max_per_day=50)
                print(f"✓ Limits set: {client.limits().max_per_tx}/tx, {client.limits().max_per_day}/day")
                
                # Step 2: Discover services
                services = client.discover("https://juai8.com/zen7")
                print(f"✓ Discovered {len(services)} services:")
                for svc in services:
                    print(f"  - {svc.id}: ${svc.price} {svc.currency}")
                
                assert len(services) == 3
                
                # Step 3: Select service based on criteria (cheapest video service)
                video_services = [s for s in services if "video" in s.id.lower()]
                cheapest = min(video_services, key=lambda s: s.price)
                print(f"✓ Selected: {cheapest.id} (${cheapest.price})")
                
                assert cheapest.id == "text-to-video"
                assert cheapest.price == 0.99
                
                # Step 4: Check if within budget
                limits = client.limits()
                assert cheapest.price <= limits.max_per_tx, "Service too expensive"
                assert cheapest.price <= limits.remaining_daily, "Would exceed daily limit"
                print(f"✓ Budget check passed")
                
                # Step 5: Pay and call service
                result = client.pay(
                    "https://juai8.com/zen7",
                    cheapest.id,
                    prompt="A majestic dragon flying over mountains at sunset"
                )
                
                # Step 6: Verify result
                assert result.success
                assert result.amount == 0.99
                assert "video_url" in result.result
                print(f"✓ Payment successful: ${result.amount}")
                print(f"✓ Video URL: {result.result['video_url']}")
                
                # Step 7: Verify spending tracked
                assert client.limits().spent_today == 0.99
                print(f"✓ Spending tracked: ${client.limits().spent_today} today")
                
                # Return the video URL (what the agent would use)
                video_url = result.result["video_url"]
                assert video_url == "https://cdn.zen7.com/videos/abc123.mp4"
                
                print("\n✅ Complete flow successful!")
    
    def test_multi_payment_daily_tracking(self, tmp_path):
        """Test multiple payments within daily limit."""
        wallet_path = tmp_path / "wallet.json"
        
        discover_response = Mock()
        discover_response.status_code = 200
        discover_response.raise_for_status = Mock()
        discover_response.json = Mock(return_value={
            "services": [{"id": "service", "price": 1.0, "currency": "USDC"}]
        })
        
        def create_payment_mocks():
            r402 = Mock()
            r402.status_code = 402
            r402.headers = {"X-Payment-Required": json.dumps({
                "amount": "1.0", "currency": "USDC", "payTo": "0x1234567890123456789012345678901234567890"
            })}
            r402.json = Mock(return_value={})
            
            r200 = Mock()
            r200.status_code = 200
            r200.json = Mock(return_value={"ok": True})
            
            return [r402, r200]
        
        with patch.object(httpx.Client, 'get', return_value=discover_response):
            client = MoltsPay(wallet_path=str(wallet_path))
            client.set_limits(max_per_tx=5, max_per_day=3)  # Can do 3 payments of $1
            
            # Payment 1
            with patch.object(httpx.Client, 'post', side_effect=create_payment_mocks()):
                result = client.pay("https://example.com", "service")
                assert result.success
                assert client.limits().spent_today == 1.0
            
            # Payment 2
            with patch.object(httpx.Client, 'post', side_effect=create_payment_mocks()):
                result = client.pay("https://example.com", "service")
                assert result.success
                assert client.limits().spent_today == 2.0
            
            # Payment 3
            with patch.object(httpx.Client, 'post', side_effect=create_payment_mocks()):
                result = client.pay("https://example.com", "service")
                assert result.success
                assert client.limits().spent_today == 3.0
            
            # Payment 4 should fail - exceeds daily limit
            with pytest.raises(LimitExceeded) as exc_info:
                client.pay("https://example.com", "service")
            
            assert exc_info.value.limit_type == "daily"
            assert client.limits().spent_today == 3.0  # Unchanged
