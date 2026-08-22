import gateway
print('_WEATHER_KEYWORDS exists:', hasattr(gateway, '_WEATHER_KEYWORDS'))
print('_weather_keyword_hit exists:', hasattr(gateway, '_weather_keyword_hit'))
print('_inject_context exists:', hasattr(gateway, 'GatewayProtocol._inject_context'))
print('_handle_chat exists:', hasattr(gateway, 'GatewayProtocol._handle_chat'))
print('_handle_chat_with_tool_loop exists:', hasattr(gateway, 'GatewayProtocol._handle_chat_with_tool_loop'))

# 测试关键词命中
print('关键词测试:')
print('  "今天好热":', gateway._weather_keyword_hit('今天好热'))
print('  "下雨了":', gateway._weather_keyword_hit('下雨了'))
print('  "你好":', gateway._weather_keyword_hit('你好'))
print('  "韶关天气":', gateway._weather_keyword_hit('韶关天气'))
