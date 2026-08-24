"""
MoltsPay LangChain Integration

Usage:
    pip install moltspay[langchain]
    
    from moltspay.integrations.langchain import MoltsPayTool
    
    tools = [MoltsPayTool()]
    agent = initialize_agent(tools, llm, agent=AgentType.OPENAI_FUNCTIONS)
    agent.run("Generate a video of a cat dancing")
"""

try:
    from langchain_core.tools import BaseTool
    from langchain_core.callbacks import CallbackManagerForToolRun
except ImportError:
    raise ImportError(
        "LangChain integration requires 'langchain-core'. "
        "Install with: pip install moltspay[langchain]"
    )

from typing import Optional, Type
from pydantic import BaseModel, Field

from moltspay import MoltsPay


class MoltsPayInput(BaseModel):
    """Input schema for MoltsPay tool."""
    
    provider_url: str = Field(
        description="Provider HTTP or HTTPS URL (e.g., 'https://juai8.com/zen7')"
    )
    service_id: str = Field(
        description="Service ID to call (e.g., 'text-to-video', 'image-to-video')"
    )
    prompt: Optional[str] = Field(
        default=None,
        description="Prompt or instructions for the service"
    )


class MoltsPayTool(BaseTool):
    """
    LangChain tool for paying and using AI services via MoltsPay.
    
    This tool allows LangChain agents to:
    - Pay for AI services using USDC (gasless)
    - Generate videos, images, and other AI content
    - Call any MoltsPay-compatible service
    
    Example:
        from moltspay.integrations.langchain import MoltsPayTool
        from langchain.agents import initialize_agent, AgentType
        from langchain_openai import ChatOpenAI
        
        llm = ChatOpenAI(model="gpt-4")
        tools = [MoltsPayTool()]  # Default: Base
        # Or: tools = [MoltsPayTool(chain="polygon")]
        
        agent = initialize_agent(
            tools, 
            llm, 
            agent=AgentType.OPENAI_FUNCTIONS,
            verbose=True
        )
        
        result = agent.run("Generate a video of a cat dancing on the beach")
    """
    
    name: str = "moltspay"
    description: str = """Pay for and use AI services via MoltsPay. Use this when you need to:
- Generate videos from text descriptions (text-to-video)
- Animate images (image-to-video)
- Call any paid AI service that accepts MoltsPay

Input should include:
- provider_url: The service provider HTTP or HTTPS URL (e.g., 'https://juai8.com/zen7')
- service_id: The service to call (e.g., 'text-to-video')
- prompt: Description or instructions for the service

Returns the service result (e.g., video URL) or error message."""

    args_schema: Type[BaseModel] = MoltsPayInput
    return_direct: bool = False
    
    _client: Optional[MoltsPay] = None
    chain: str = "base"
    
    def __init__(self, chain: str = "base", **kwargs):
        super().__init__(**kwargs)
        self.chain = chain
        self._client = MoltsPay(chain=chain)
    
    @property
    def client(self) -> MoltsPay:
        if self._client is None:
            self._client = MoltsPay()
        return self._client
    
    def _run(
        self,
        provider_url: str,
        service_id: str,
        prompt: Optional[str] = None,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        """Execute the MoltsPay payment and service call."""
        try:
            result = self.client.pay(provider_url, service_id, prompt=prompt)
            
            if result.success:
                return f"✅ Success! Paid ${result.amount} USDC. Result: {result.result}"
            else:
                return f"❌ Failed: {result.error}"
                
        except Exception as e:
            return f"❌ Error: {str(e)}"


class MoltsPayDiscoverTool(BaseTool):
    """
    LangChain tool for discovering available MoltsPay services.
    
    Use this to find out what services a provider offers before paying.
    
    Example:
        tools = [MoltsPayDiscoverTool()]  # Default: Base
        # Or: tools = [MoltsPayDiscoverTool(chain="polygon")]
    """
    
    name: str = "moltspay_discover"
    description: str = """Discover available services from a MoltsPay provider.
Use this to see what services are available and their prices before paying.

Input: provider_url (e.g., 'https://juai8.com/zen7' or 'http://localhost:8402')

Returns a list of available services with names, descriptions, and prices."""
    
    _client: Optional[MoltsPay] = None
    chain: str = "base"
    
    def __init__(self, chain: str = "base", **kwargs):
        super().__init__(**kwargs)
        self.chain = chain
        self._client = MoltsPay(chain=chain)
    
    @property
    def client(self) -> MoltsPay:
        if self._client is None:
            self._client = MoltsPay()
        return self._client
    
    def _run(
        self,
        provider_url: str,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        """Discover services from a provider."""
        try:
            services = self.client.discover(provider_url)
            
            if not services:
                return f"No services found at {provider_url}"
            
            result = f"Available services at {provider_url}:\n\n"
            for svc in services:
                result += f"• **{svc.name}** ({svc.id})\n"
                result += f"  {svc.description}\n"
                result += f"  Price: ${svc.price} {svc.currency}\n\n"
            
            return result
            
        except Exception as e:
            return f"❌ Error discovering services: {str(e)}"


# Convenience function to get all MoltsPay tools
def get_moltspay_tools(chain: str = "base") -> list:
    """
    Get all MoltsPay tools for LangChain.
    
    Args:
        chain: Blockchain to use ("base" or "polygon"). Default: "base"
    
    Returns:
        List of [MoltsPayTool, MoltsPayDiscoverTool]
    
    Example:
        from moltspay.integrations.langchain import get_moltspay_tools
        
        # Default: Base
        tools = get_moltspay_tools()
        
        # Or use Polygon
        tools = get_moltspay_tools(chain="polygon")
        
        agent = initialize_agent(tools, llm, ...)
    """
    return [MoltsPayTool(chain=chain), MoltsPayDiscoverTool(chain=chain)]
