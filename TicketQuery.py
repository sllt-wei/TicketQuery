import requests
import openai
import re
import plugins
from plugins import *
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from datetime import datetime, timedelta
from collections import defaultdict

# 配置信息
# open_ai_api_key = "填写你的OpenAI API Key"
# model = "gpt-3.5-turbo"
# open_ai_api_base = "https://api.openai.com/v1"

BASE_URL_HIGHSPEEDTICKET = "https://api.pearktrue.cn/api/highspeedticket"

@plugins.register(name="TicketQuery",
                  desc="智能票务查询插件",
                  version="0.1",
                  author="sllt",
                  desire_priority=100)
class TicketQuery(Plugin):
    content = None
    ticket_info_list = []
    intermediate_ticket_info_list = []
    conversation_history = []
    last_interaction_time = None

    def __init__(self):
        super().__init__()
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        
        # 初始化分页相关属性
        self.current_page = 1
        self.page_size = 10  # 每页显示10条
        self.total_data = []  # 保存完整查询结果
        self.last_query_params = None  # 保存上次查询参数
        logger.info(f"[{__class__.__name__}] 插件初始化完成")

    def get_help_text(self, **kwargs):
        help_text = """【使用说明】
1. 基础查询（显示前10条）：
   - 票种 出发地 终点地 （例：高铁 北京 上海）
   - 票种 出发地 终点地 日期 （例：高铁 北京 上海 2024-06-05）
   - 票种 出发地 终点地 日期 时间 （例：高铁 北京 上海 2024-06-05 09:00）

2. 自然语言查询：
   - "查明天上午从北京到上海的高铁"
   - "今天下午3点的高铁从北京到上海"
   
3. 分页操作：
   - +下一页：查看后续结果
   - +上一页：返回前页结果

4. 后续筛选：
   - +最便宜的二等座
   - +上午出发的车次

5. 中转查询：
   - 中转+高铁 成都 上海 2024-06-05 09:00"""
        return help_text

    def on_handle_context(self, e_context: EventContext):
        if e_context['context'].type != ContextType.TEXT:
            return
            
        self.content = e_context["context"].content.strip()
        logger.debug(f"收到查询内容：{self.content}")

        # 处理分页命令
        if self.content in ["+下一页", "+上一页"]:
            self._handle_pagination(e_context)
            return

        # 清理10分钟前的历史记录
        if self.last_interaction_time and datetime.now() - self.last_interaction_time > timedelta(minutes=10):
            self.conversation_history.clear()
            self.ticket_info_list.clear()
            self.intermediate_ticket_info_list.clear()
            logger.debug("已清除过期对话历史")

        self.last_interaction_time = datetime.now()

        # 自然语言解析增强
        if any(keyword in self.content for keyword in ["高铁", "动车", "普通"]) and "从" in self.content and "到" in self.content:
            logger.debug("开始处理自然语言查询")
            self._process_natural_language()

        # 处理后续筛选问题
        if self.content.startswith("+"):
            logger.debug("开始处理后续筛选问题")
            self._handle_followup_question(e_context)
            return

        # 处理主查询
        if self.content.split()[0] in ["高铁", "普通", "动车"]:
            logger.debug("开始处理主查询")
            self._handle_main_query(e_context)

    def _process_natural_language(self):
        """处理自然语言查询"""
        try:
            logger.debug("开始解析自然语言")
            ticket_type = "高铁" if "高铁" in self.content else "动车" if "动车" in self.content else "普通"
            
            # 使用正则表达式提取关键信息
            from_match = re.search(r"从(.+?)到", self.content)
            to_match = re.search(r"到(.+?)(?:的|票|时间|车次|$)", self.content)
            date_match = re.search(r"(\d{4}年?\d{1,2}月?\d{1,2}日?|今天|明天|后天)", self.content)
            time_match = re.search(r"(\d{1,2}[:：]\d{2})(?:点|分|钟|左右)?", self.content)

            # 构建参数列表
            parts = [ticket_type]
            if from_match and to_match:
                parts.append(from_match.group(1).strip())
                parts.append(to_match.group(1).strip())
                
                # 处理日期
                now = datetime.now()
                if date_match:
                    date_str = date_match.group(1)
                    if "今天" in date_str:
                        query_date = now.strftime("%Y-%m-%d")
                    elif "明天" in date_str:
                        query_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                    elif "后天" in date_str:
                        query_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")
                    else:
                        # 转换中文日期格式
                        date_str = re.sub(r"[年月日]", "-", date_str).strip("-")
                        query_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
                    parts.append(query_date)
                    
                # 处理时间
                if time_match:
                    time_str = time_match.group(1).replace("：", ":")
                    parts.append(time_str)

            self.content = " ".join(parts)
            logger.debug(f"自然语言转换结果：{self.content}")
        except Exception as e:
            logger.error(f"自然语言解析失败：{e}")

    def _handle_main_query(self, e_context):
        """处理主查询请求"""
        logger.debug(f"处理主查询：{self.content}")
        parts = self.content.split()
        if len(parts) not in [3, 4, 5]:
            self._send_error("参数数量不正确，请检查查询格式", e_context)
            return

        try:
            # 解析基础参数
            ticket_type = parts[0]
            from_location = parts[1]
            to_location = parts[2]
            now = datetime.now()
            
            # 初始化查询参数
            query_date = now.strftime("%Y-%m-%d")
            query_time = ""
            
            # 处理可选参数
            if len(parts) >= 4:
                param = parts[3]
                # 判断是日期还是时间
                if re.match(r"\d{4}-\d{2}-\d{2}", param):
                    query_date = param
                elif re.match(r"\d{1,2}:\d{2}", param):
                    query_time = param
                else:
                    raise ValueError("日期/时间格式错误")
                
            if len(parts) == 5:
                query_time = parts[4]

            # 记录查询历史
            self.conversation_history.append({
                "role": "user",
                "content": f"查询{ticket_type} {from_location}到{to_location}，日期{query_date}，时间{query_time or '全天'}"
            })
            logger.debug(f"查询历史记录：{self.conversation_history[-1]}")

            full_data = self.get_ticket_info(ticket_type, from_location, to_location, query_date, query_time)
            
            # 获取票务信息
            result = self.get_ticket_info(
                ticket_type, 
                from_location, 
                to_location,
                query_date,
                query_time
            )
            
        
            if full_data:
                # 保存完整数据和查询参数
                self.total_data = full_data
                self.last_query_params = (ticket_type, from_location, to_location, query_date, query_time)
                self.current_page = 1  # 重置页码
            
                # 显示第一页
                page_data = self._get_current_page()
                reply_content = self._format_response(page_data)
            else:
                reply_content = "未找到符合条件的车次"
            # 处理结果
            reply = Reply()
            reply.type = ReplyType.TEXT if full_data else ReplyType.ERROR
            reply.content = reply_content
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

        except ValueError as ve:
            logger.error(f"参数格式错误：{ve}")
            self._send_error(f"参数格式错误：{ve}", e_context)
        except Exception as e:
            logger.error(f"查询处理异常：{e}")
            self._send_error("查询服务暂时不可用，请稍后再试", e_context)

    def get_ticket_info(self, ticket_type, from_loc, to_loc, date, time=""):
        """调用票务API获取数据"""
        logger.debug(f"调用票务API：{BASE_URL_HIGHSPEEDTICKET}，参数：{from_loc} 到 {to_loc}，日期：{date}，时间：{time}")
        params = {
            "from": from_loc,
            "to": to_loc,
            "time": date
        }
        
        try:
            resp = requests.get(BASE_URL_HIGHSPEEDTICKET, params=params, timeout=5)
            logger.debug(f"API响应：{resp.status_code}，内容：{resp.text}")
            if resp.status_code == 200:
                data = resp.json()
                logger.debug(f"原始API数据：{json.dumps(data, ensure_ascii=False)}")  # 新增调试日志
                if data.get('code') == 200:
                    return self._process_api_data(data.get('data', []), ticket_type, time)
                else:
                    logger.error(f"API返回错误：{data.get('msg')}")
                    return None
            return None
        except Exception as e:
            logger.error(f"API请求失败：{e}")
            return None

    # def _process_api_data(self, data, ticket_type, query_time):
    #     """增强数据处理"""
    #     filtered = []
    #     for item in data:
    #         # 确保包含必要字段
    #         if not all(key in item for key in ['departstation', 'arrivestation', 'runtime']):
    #             continue
            
    #     """处理API返回数据"""
    #     logger.debug(f"处理API返回的数据：{data}")
    #     filtered = []
    #     for item in data:
    #         if item.get('traintype', '').lower() != ticket_type.lower():
    #             continue
                
    #         depart_time = datetime.strptime(item['departtime'], "%H:%M").time()
            
    #         # 时间筛选
    #         if query_time:
    #             query_time_obj = datetime.strptime(query_time, "%H:%M").time()
    #             if depart_time < query_time_obj:
    #                 continue
                    
    #         filtered.append(item)
            
    #         # 添加票价统计信息
    #         min_price = None
    #         max_price = None
    #         for seat in item.get('ticket_info', []):
    #             price = seat.get('seatprice')
    #             if price:
    #                 price = float(price)
    #                 min_price = min(min_price, price) if min_price else price
    #                 max_price = max(max_price, price) if max_price else price
    #         item['price_range'] = f"¥{min_price}-{max_price}" if min_price and max_price else "价格未知"
            
    #         filtered.append(item)
            
    #     # 按出发时间排序
    #     return sorted(filtered, key=lambda x: x['departtime'])
    
    def _process_api_data(self, data, ticket_type, query_time):
        """处理API返回数据"""
        logger.debug(f"处理API返回的数据：{data}")
        seen_trains = set()  # 用于去重的集合
        filtered = []
        for item in data:
            # 1. 字段完整性校验
            required_fields = ['trainumber', 'departtime', 'arrivetime', 'traintype']
            if not all(key in item for key in required_fields):
                logger.debug(f"缺失必要字段，跳过条目：{item}")
                continue

            # 2. 去重判断（按车次+出发时间）
            unique_key = f"{item['trainumber']}_{item['departtime']}_{item['arrivetime']}"
            if unique_key in seen_trains:
                logger.debug(f"跳过重复车次：{unique_key}")
                continue
            seen_trains.add(unique_key)
        
            # 3. 类型筛选
            if item['traintype'].lower() != ticket_type.lower():
                continue
            
            # 4. 时间筛选
            depart_time = datetime.strptime(item['departtime'], "%H:%M").time()
            if query_time:
                try:
                    query_time_obj = datetime.strptime(query_time, "%H:%M").time()
                    if depart_time < query_time_obj:
                        continue
                except ValueError:
                    logger.warning(f"无效的时间格式: {query_time}")
                    
            # 5. 仅添加一次
            filtered.append(item)
            logger.debug(f"数据: {item}")

        #按出发时间排序
        return sorted(filtered, key=lambda x: x['departtime'])
        
    def _handle_pagination(self, e_context):
        """处理分页请求"""
        if not self.total_data:
            self._send_error("请先进行车次查询", e_context)
            return

        # 计算总页数
        total_pages = (len(self.total_data) + self.page_size - 1) // self.page_size

        if self.content == "+下一页":
            if self.current_page < total_pages:
                self.current_page += 1
            else:
                self._send_error("已经是最后一页了", e_context)
                return
        elif self.content == "+上一页":
            if self.current_page > 1:
                self.current_page -= 1
            else:
                self._send_error("已经是第一页了", e_context)
                return

        # 获取当前页数据
        page_data = self._get_current_page()
        reply = Reply()
        reply.type = ReplyType.TEXT
        reply.content = self._format_response(page_data)
        e_context["reply"] = reply
        e_context.action = EventAction.BREAK_PASS

    def _get_current_page(self):
        """获取当前页数据"""
        start = (self.current_page - 1) * self.page_size
        end = start + self.page_size
        return self.total_data[start:end]
        
    def _format_response(self, page_data):
        # """格式化分页响应"""
        # if not page_data:
        #     return "没有更多车次信息"
            
        # result = []
        # global_index = (self.current_page - 1) * self.page_size + 1
        # for idx, item in enumerate(page_data, global_index):
        #     info = f"{idx}. 【{item['trainumber']}】{item['departtime']} - {item['arrivetime']}\n"
        #     info += f"   历时：{item['runtime']} | 席位："
        #     info += "，".join([f"{s['seatname']}({s['seatinventory']}张)" 
        #                     for s in item.get('ticket_info', [])])
        #     result.append(info)
            
        # footer = f"\n第 {self.current_page}/{(len(self.total_data)+self.page_size-1)//self.page_size} 页"
        # footer += "\n发送【+下一页】查看后续结果\n发送【+筛选条件】进行筛选"
        # return "\n\n".join(result) + footer
        
        if not page_data:
            return "没有更多车次信息"

        result = []
        global_index = (self.current_page - 1) * self.page_size + 1
        for idx, item in enumerate(page_data, global_index):
            info = f"{idx}. 【{item.get('trainumber', '未知车次')}】{item.get('traintype', '未知类型')}\n"
            info += f"   🚩出发站：{item.get('departstation', '未知')} ➔ 到达站：{item.get('arrivestation', '未知')}\n"
            info += f"   ⏰时间：{item.get('departtime', '未知')} - {item.get('arrivetime', '未知')}（历时：{item.get('runtime', '未知')}\n"
            
            # 处理票价信息
            seats = item.get('ticket_info', [])
            if seats:
                seat_info = "   💺席位："
                seat_info += " | ".join([
                    f"{s.get('seatname', '未知')}：¥{s.get('seatprice', '未知')}（余{s.get('seatinventory', 0)}张）"
                    for s in seats
                ])
                info += seat_info + "\n"
            else:
                info += "   ⚠️暂无余票信息\n"
            
            result.append(info)
            
        total_pages = (len(self.total_data) + self.page_size - 1) // self.page_size
        footer = f"\n📄第 {self.current_page}/{total_pages} 页"
        footer += "\n🔍发送【+下一页】查看后续结果" if self.current_page < total_pages else ""
        footer += "\n🎯发送【+筛选条件】进行精确筛选（如：+二等座低于500元）"
        return "\n".join(result) + footer

    def _ai_filter(self, question):
        """使用OpenAI进行筛选（基于完整数据）"""
        # 注意这里使用self.total_data而不是self.ticket_info_list
        openai.api_key = open_ai_api_key
        openai.api_base = open_ai_api_base

        prompt = f"""根据用户要求筛选车次：
用户要求：{question}
当前车次信息包含以下字段：
- 出发站/到达站
- 车辆类型
- 发车/到达时间
- 历时
- 价格区间
- 席位信息

请用JSON返回符合要求的车次ID列表：
{{"selection": ["车次号1", "车次号2"], "reason": "筛选理由"}}"""

        try:
            resp = openai.ChatCompletion.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            result = resp.choices[0].message['content']
            selected_ids = self._parse_ai_response(result)
            
            # 筛选后重新加载数据
            self.total_data = [item for item in self.total_data 
                             if item['trainumber'] in selected_ids]
            self.current_page = 1  # 筛选后重置页码
            return self._get_current_page()
        except:
            return self.total_data[:self.page_size]

    # def _format_response(self, data):
    #     """格式化返回结果"""
    #     if not data:
    #         return "未找到符合条件的车次信息"
            
    #     result = []
    #     for idx, item in enumerate(data[:5], 1):
    #         info = f"{idx}. 【{item['trainumber']}】{item['departtime']} - {item['arrivetime']}\n"
    #         info += f"   历时：{item['runtime']} | 席位："
    #         info += "，".join([f"{s['seatname']}({s['seatinventory']}张)" 
    #                         for s in item.get('ticket_info', [])])
    #         result.append(info)
            
    #     return "\n\n".join(result) + "\n\n发送【+筛选条件】继续筛选"

    def _handle_followup_question(self, e_context):
        """处理后续筛选问题"""
        logger.debug(f"收到筛选问题：{self.content}")
        question = self.content[1:].strip()
        
        if not self.ticket_info_list:
            self._send_error("请先进行车次查询", e_context)
            return
            
        try:
            # 使用OpenAI进行智能筛选
            filtered = self._ai_filter(question)
            reply = Reply()
            
            if filtered:
                reply.type = ReplyType.TEXT
                reply.content = self._format_response(filtered)
                self.ticket_info_list = filtered  # 更新结果集
            else:
                reply.type = ReplyType.ERROR
                reply.content = "没有符合筛选条件的车次"
                
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
        except Exception as e:
            logger.error(f"筛选处理失败：{e}")
            self._send_error("筛选服务暂时不可用", e_context)

    def _ai_filter(self, question):
        """使用OpenAI进行智能筛选"""
        logger.debug(f"使用AI筛选：{question}")
        openai.api_key = open_ai_api_key
        openai.api_base = open_ai_api_base

        prompt = f"""根据用户要求筛选车次：
用户要求：{question}
当前车次信息：
{self._format_for_ai(self.ticket_info_list)}

请用JSON格式返回符合要求的车次ID列表，格式如：
{{"selection": ["G123", "D456"]}}"""

        try:
            resp = openai.ChatCompletion.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            result = resp.choices[0].message['content']
            selected_ids = self._parse_ai_response(result)
            logger.debug(f"AI筛选结果：{selected_ids}")
            
            return [item for item in self.ticket_info_list 
                   if item['trainumber'] in selected_ids]
        except Exception as e:
            logger.error(f"AI筛选失败：{e}")
            return self.ticket_info_list  # 失败时返回原始数据

    def _format_for_ai(self, data):
        """为AI处理格式化数据"""
        return "\n".join([
            f"{item['trainumber']} | {item['traintype']} | "
            f"{item['departstation']}→{item['arrivestation']} | "
            f"{item['departtime']}-{item['arrivetime']} | "
            f"价格区间：{item.get('price_range', '未知')} | "
            "席位：" + "/".join([f"{s['seatname']}({s['seatinventory']})" for s in item['ticket_info']])
            for item in data
        ])

    def _parse_ai_response(self, text):
        """解析AI返回的JSON"""
        try:
            import json
            data = json.loads(text)
            return data.get('selection', [])
        except Exception as e:
            logger.error(f"AI返回解析失败：{e}")
            return []

    def _send_error(self, message, e_context):
        """发送错误信息"""
        logger.error(f"错误信息：{message}")
        reply = Reply()
        reply.type = ReplyType.ERROR
        reply.content = message
        e_context["reply"] = reply
        e_context.action = EventAction.BREAK_PASS
