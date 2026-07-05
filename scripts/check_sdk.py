import asyncio
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

async def main():
    options = ClaudeAgentOptions(
        cwd=".",
        allowed_tools=["Read", "Glob", "Grep"],
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query("Say hello and tell me what directory you're running in.")
        async for message in client.receive_response():
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text"):
                        print(block.text)

asyncio.run(main())