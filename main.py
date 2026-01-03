import aiohttp
import json
import asyncio
from pathlib import Path
from rapidfuzz import process, fuzz
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
import astrbot.api.message_components as Comp
from apscheduler.schedulers.asyncio import AsyncIOScheduler


@register(
    "astrbot_plugin_SteamSaleTracker",
    "bushikq",
    "一个监控steam游戏价格变动的astrbot插件",
    "1.1.5",
)
class SteamSaleTrackerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_SteamSaleTracker"))
        self.plugin_dir = Path(__file__).resolve().parent
        self.json1_path = (
            self.data_dir / "game_list.json"
        )  # 存储所有Steam游戏英文名与id对应的字典
        self.json2_path = (
            self.data_dir / "monitor_list.json"
        )  # 存储用户或群组的监控列表

        # 确保数据文件存在，如果不存在则创建空文件
        if not self.json1_path.exists():
            with open(self.json1_path, "w", encoding="utf-8") as f:
                json.dump({}, f)
        if not self.json2_path.exists():
            with open(self.json2_path, "w", encoding="utf-8") as f:
                json.dump({}, f)

        self.config = config
        # 从配置中获取价格检查间隔时间，默认为 30 分钟
        self.interval_minutes = self.config.get("interval_minutes", 30)

        # 通过https://store.steampowered.com/pointssummary/ajaxgetasyncconfig获取access_token密钥(废案)
        # 通过https://steamcommunity.com/dev/apikey获取apikey(推荐)
        self.steam_api_key = self.config.get("steam_api_key")

        logger.info("正在初始化SteamSaleTracker插件")

        self.scheduler = AsyncIOScheduler()
        # 添加定时任务，每隔 interval_minutes 运行 run_monitor_prices 方法
        self.scheduler.add_job(
            self.run_monitor_prices, "interval", minutes=self.interval_minutes
        )
        self.scheduler.start()  # 启动调度器

        self.monitor_list_lock = asyncio.Lock()  # 用于保护 monitor_list 文件的读写
        self.data_initialized = asyncio.Event()  # 添加一个Event来标记数据是否初始化完成
        asyncio.create_task(self.initialize_data())  # 异步初始化数据，避免阻塞主线程

    async def initialize_data(self):
        """异步初始化数据（避免阻塞主线程），获取游戏列表和加载用户监控列表"""
        await self.get_app_list()  # 获取Steam全量游戏列表
        await self.load_user_monitors()  # 加载用户监控列表
        self.data_initialized.set()  # 设置Event，表示数据初始化完成

    async def get_app_list(self):
        """获取Steam全量游戏列表（AppID + 名称），并缓存到 game_list.json"""
        logger.info("开始获取 Steam 全量游戏列表...")

        all_apps = []
        last_appid = 0
        have_more_results = True
        max_results = 50000

        # 规范链接，方便后续维护
        # 排除硬件和视频类型
        base_url = (
            f"https://api.steampowered.com/IStoreService/GetAppList/v1/"
            f"?key={self.steam_api_key}"
            f"&max_results={max_results}"
            f"&include_games=true"
            f"&include_dlc=true"
            f"&include_software=true"
            f"&include_videos=false"
            f"&include_hardware=false"
        )

        try:
            async with aiohttp.ClientSession() as session:
                while have_more_results:
                    # 拼接 last_appid
                    url = f"{base_url}&last_appid={last_appid}"

                    async with session.get(url) as response:
                        if response.status != 200:
                            logger.error(
                                f"请求 Steam AppList 失败，状态码: {response.status}"
                            )
                            break

                        res = await response.json()

                        if not res or "response" not in res:
                            logger.error("Steam API 返回数据格式异常")
                            break

                        data = res["response"]
                        apps = data.get("apps", [])

                        if apps:
                            all_apps.extend(apps)
                            logger.info(f"已获取 {len(all_apps)} 个应用...")

                        # 获取have_more_results 和 last_appid
                        have_more_results = data.get("have_more_results", False)

                        if have_more_results:  # 还有剩余
                            last_appid = data.get("last_appid")
                            if last_appid is None:
                                # 以防万一 API 抽风没给 last_appid，兜底使用列表最后一个
                                last_appid = apps[-1]["appid"] if apps else 0

                        # 避免请求过快
                        await asyncio.sleep(0.2)

            # 数据处理
            self.app_dict_all = {
                app["name"]: app["appid"] for app in all_apps if "name" in app
            }
            self.app_dict_all_reverse = {v: k for k, v in self.app_dict_all.items()}

            with open(self.json1_path, "w", encoding="utf-8") as f:
                json.dump(self.app_dict_all, f, ensure_ascii=False, indent=4)

            logger.info(
                f"Steam游戏列表更新成功，共加载 {len(self.app_dict_all)} 个游戏。"
            )

        except Exception as e:
            logger.error(f"获取游戏列表失败：{e}")
            self.app_dict_all = {}
        finally:
            if not self.app_dict_all and self.json1_path.exists():
                try:
                    with open(self.json1_path, "r", encoding="utf-8") as f:
                        self.app_dict_all = json.load(f)
                    self.app_dict_all_reverse = {
                        v: k for k, v in self.app_dict_all.items()
                    }
                    logger.info("已回退使用本地缓存的游戏列表")
                except Exception:
                    logger.error("本地缓存的游戏列表加载失败")

    async def load_user_monitors(self):
        """加载用户监控列表（从 monitor_list.json 文件）"""
        try:
            async with self.monitor_list_lock:  # 加锁读取，防止文件被其他操作同时修改
                with open(self.json2_path, "r", encoding="utf-8") as f:
                    self.monitor_list = json.load(f)
            logger.info("监控列表加载成功")
        except (FileNotFoundError, json.JSONDecodeError) as e:  # 组合异常捕获
            self.monitor_list = {}  # 文件不存在或者文件损坏时初始化为空字典
            logger.info(f"监控列表文件不存在或损坏，已创建空列表: {e}")
            with open(self.json2_path, "w", encoding="utf-8") as f:
                json.dump({}, f)

    async def get_appid_by_name(self, user_input, target_dict: dict = None):
        """
        模糊匹配游戏名到AppID。
        Args:
            user_input (str): 用户输入的待匹配游戏名。
            target_dict (dict): 用于匹配的目标字典 (游戏名: AppID)。
        Returns:
            list or None: 如果找到匹配项，返回 [AppID, 匹配的游戏名]，否则返回 None。
        """
        logger.info(f"正在模糊匹配游戏名: {user_input}")
        # 等待数据初始化完成
        await self.data_initialized.wait()
        if not target_dict:  # 如果没有传入target_dict，则默认使用app_dict_all
            target_dict = self.app_dict_all  # 这里就确保了self.app_dict_all是可用的
        if not target_dict:  # 再次检查，以防初始化失败
            logger.warning(
                "target_dict 和 self.app_dict_all 都为空，无法进行模糊匹配。"
            )
            return None

        matched_result = process.extractOne(
            user_input, target_dict.keys(), scorer=fuzz.token_set_ratio
        )
        if matched_result and matched_result[1] >= 70:
            matched_name = matched_result[0]
            return [target_dict[matched_name], matched_name]
        else:
            return None

    async def get_steam_price(self, appid, region="cn"):
        """
        获取游戏价格信息。
        Args:
            appid (str or int): Steam 游戏的 AppID。
            region (str): 区域代码，默认为 "cn" (中国)。
        Returns:
            dict or None: 包含价格信息的字典，或 None（如果获取失败或游戏不存在）。
        """
        try:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc={region}&l=zh-cn"
            async with aiohttp.ClientSession() as session:  # 使用 aiohttp 替代 requests
                async with session.get(url) as response:
                    res = await response.json()

            data = res.get(str(appid))
            if not data or not data.get("success"):
                logger.warning(f"获取游戏 {appid} 价格失败或游戏不存在，data: {data}")
                return None

            game_data = data["data"]
            if game_data.get("is_free"):  # 免费游戏
                return {
                    "is_free": True,
                    "current_price": 0,
                    "original_price": 0,
                    "discount": 100,
                    "currency": "FREE",
                }

            price_info = game_data.get("price_overview")
            if not price_info:
                logger.info(
                    f"游戏 {game_data.get('name', appid)} 没有价格信息 (可能即将发售或未在 {region} 区域上架)。"
                )
                return None

            return {
                "is_free": False,
                "current_price": price_info["final"] / 100,  # 单位转换为元
                "original_price": price_info["initial"] / 100,
                "discount": price_info["discount_percent"],
                "currency": price_info["currency"],  # 货币类型
            }
        except Exception as e:
            logger.error(f"获取游戏 {appid} 价格时发生异常：{e}")
            return None

    def _parse_unified_origin(self, origin: str):
        """
        解析 unified_msg_origin 字符串，提取平台、消息类型、用户ID和群ID。
        格式示例: aiocqhttp:FriendMessage:UserID
                  aiocqhttp:GroupMessage:UserID_GroupID (带会话隔离)
                  aiocqhttp:GroupMessage:GroupID (不带会话隔离)
        """
        parts = origin.split(":")
        platform = parts[0]
        message_type = parts[1]
        identifiers = parts[2]

        user_id = None
        group_id = None

        if message_type == "FriendMessage":
            user_id = identifiers
        elif message_type == "GroupMessage":
            if "_" in identifiers:
                user_id, group_id = identifiers.split("_")
            else:
                group_id = identifiers

        return {
            "platform": platform,
            "message_type": message_type,
            "user_id": user_id,
            "group_id": group_id,
        }

    async def monitor_prices(self):
        """
        定时检查监控列表中游戏的价格变动。
        如果价格有变动，则 yield 通知信息。
        Yields:
            tuple: (unified_msg_origin, at_members_list, msg_components)
                   unified_msg_origin: 会话的唯一标识符
                   at_members_list: 需要在群聊中 @ 的用户ID列表 (私聊时为空)
                   msg_components: 消息组件列表
        """
        async with self.monitor_list_lock:
            try:
                with open(self.json2_path, "r", encoding="utf-8") as f:
                    current_monitor_list = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError) as e:
                logger.error(f"监控列表文件解析失败或不存在，已重置为空列表: {e}")
                current_monitor_list = {}

        games_to_check = list(current_monitor_list.items())

        for game_id, game_info in games_to_check:
            logger.info(f"正在检查游戏: {game_info['name']} (AppID: {game_id})")
            price_data = await self.get_steam_price(
                game_id, game_info.get("region", "cn")
            )

            if not price_data:
                logger.warning(
                    f"无法获取游戏《{game_info.get('name', game_id)}》的价格信息，跳过此次检查。"
                )
                continue

            # 首次设置价格，初始化 last_price 等数据
            if game_info["last_price"] is None:
                current_monitor_list[game_id]["last_price"] = price_data[
                    "current_price"
                ]
                current_monitor_list[game_id]["original_price"] = price_data[
                    "original_price"
                ]
                current_monitor_list[game_id]["discount"] = price_data["discount"]
                async with self.monitor_list_lock:
                    with open(self.json2_path, "w", encoding="utf-8") as f:
                        json.dump(current_monitor_list, f, ensure_ascii=False, indent=4)
                logger.info(
                    f"游戏《{game_info.get('name', game_id)}》首次记录价格：¥{price_data['current_price']:.2f}"
                )
                continue  # 首次记录不发送通知

            price_change = price_data["current_price"] - game_info["last_price"]

            # 如果价格有变动
            if price_change != 0:
                logger.info(f"游戏《{game_info.get('name', game_id)}》价格变动！")

                if price_data["is_free"]:
                    msg_components = [
                        (
                            Comp.Plain(
                                text=f"🎉🎉🎉游戏《{game_info['name']}》已免费！\n"
                            )
                        )
                    ]
                elif price_change > 0:
                    msg_components = [
                        Comp.Plain(
                            text=f"⬆️游戏《{game_info['name']}》价格上涨：¥{price_change:.2f}\n"
                        )
                    ]
                elif price_change < 0:
                    msg_components = [
                        (
                            Comp.Plain(
                                text=f"⬇️游戏《{game_info['name']}》价格下跌：¥{-price_change:.2f}\n"
                            )
                        )
                    ]

                msg_components.append(
                    Comp.Plain(
                        text=f"变动前价格：¥{game_info['last_price']:.2f}，当前价：¥{price_data['current_price']:.2f}，原价：¥{price_data['original_price']:.2f}，对比原价折扣：{price_data['discount']}%\n"
                    )
                )
                msg_components.append(
                    Comp.Plain(
                        text=f"购买链接：https://store.steampowered.com/app/{game_id}\n"
                    )
                )

                # 更新内存中的监控列表
                current_monitor_list[game_id]["last_price"] = price_data[
                    "current_price"
                ]
                current_monitor_list[game_id]["original_price"] = price_data[
                    "original_price"
                ]
                current_monitor_list[game_id]["discount"] = price_data["discount"]

                # 立即将更新后的监控列表写入文件
                async with self.monitor_list_lock:
                    with open(self.json2_path, "w", encoding="utf-8") as f:
                        json.dump(current_monitor_list, f, ensure_ascii=False, indent=4)

                # 遍历所有订阅者，确定通知目标
                for subscriber_origin in game_info.get("subscribers", []):
                    parsed_origin = self._parse_unified_origin(subscriber_origin)

                    if parsed_origin["message_type"] == "FriendMessage":
                        # 个人用户，直接发送私聊，不需要 @
                        yield subscriber_origin, [], msg_components
                    elif parsed_origin["message_type"] == "GroupMessage":
                        at_members = []
                        # 如果 unified_msg_origin 中包含 UserID (即有会话隔离或Go-Cqhttp/Onebot等明确提供发送者ID的情况)
                        if parsed_origin[
                            "user_id"
                        ]:  # 如果群消息来源带有用户ID，则 @ 该用户
                            at_members.append(parsed_origin["user_id"])
                        # 否则（unified_msg_origin 只有 GroupID），则不 @ 任何人，直接发群消息
                        yield subscriber_origin, at_members, msg_components
            else:
                logger.info(f"游戏《{game_info.get('name', game_id)}》价格未变动")

    async def run_monitor_prices(self):
        """定时任务的wrapper函数（迭代生成器并发送消息）"""
        logger.info("开始执行价格检查任务")
        # 等待数据初始化完成
        await self.data_initialized.wait()
        try:
            # 迭代 monitor_prices 生成器，获取所有待发送的消息
            # 接收 unified_msg_origin, at_members, msg_components
            async for (
                unified_msg_origin,
                at_members,
                msg_components,
            ) in self.monitor_prices():
                if not unified_msg_origin or not msg_components:
                    continue

                parsed_origin = self._parse_unified_origin(unified_msg_origin)

                if parsed_origin["message_type"] == "GroupMessage":
                    # 对于群聊消息，添加 @ 成员
                    if at_members:
                        for member_id in at_members:
                            msg_components.append(Comp.At(qq=member_id))
                    else:
                        # 如果是群聊但没有可 @ 的用户，记录警告
                        logger.warning(
                            f"群组 {unified_msg_origin} 订阅的游戏《{msg_components[0].text.split('《')[1].split('》')[0]}》没有指定@成员或无法解析用户ID，消息将直接发送到群里。"
                        )

                    logger.info(
                        f"正在向会话 {unified_msg_origin} (群聊) 发送价格变动通知。"
                    )
                elif parsed_origin["message_type"] == "FriendMessage":
                    # 私聊消息，不需要 @ 任何人
                    logger.info(
                        f"正在向会话 {unified_msg_origin} (私聊) 发送价格变动通知。"
                    )
                final_message_components = MessageChain(msg_components)
                # 使用 unified_msg_origin 发送消息
                await self.context.send_message(
                    unified_msg_origin,
                    final_message_components,
                )
                await asyncio.sleep(1)  # 增加1s延迟，避免被风控
            logger.info("价格检查任务执行完成")
        except Exception as e:
            logger.error(f"价格检查任务失败：{e}")

    @filter.command("steamrmd", alias={"steam订阅", "steam订阅游戏"})
    async def steamremind_command(self, event: AstrMessageEvent):
        """
        创建游戏监控。
        若游戏价格变动则提醒，群组订阅在群内提醒，个人订阅私聊提醒。
        """
        # 在这里等待数据初始化完成
        await self.data_initialized.wait()
        if not self.app_dict_all:  # 如果数据初始化失败，app_dict_all为空，直接返回错误
            yield event.plain_result("游戏列表数据未加载完成或加载失败，请稍后再试。")
            return

        region = "cn"
        args = event.message_str.strip().split()[1:]
        if len(args) < 1:
            yield event.plain_result("请输入游戏名，例如：/steam订阅 Cyberpunk 2077")
            return
        elif len(args) == 1 and str.isdecimal(args[0]):
            # 输入的是 appid
            app_id = args[0]
            if int(app_id) not in self.app_dict_all_reverse:
                yield event.plain_result(f"未找到 AppID 为 {app_id} 的游戏。")
                return
            game_info_list = [app_id, self.app_dict_all_reverse[int(app_id)]]
        else:
            app_name = " ".join(args)
            yield event.plain_result(f"正在搜索 {app_name}，请稍候...")
            game_info_list = await self.get_appid_by_name(app_name, self.app_dict_all)
        logger.info(f"搜索结果 game_info_list: {game_info_list}")

        if not game_info_list:
            yield event.plain_result(
                f"未找到《{app_name}》，请检查拼写或尝试更精确的名称。(目前仅支持英文名称和应用id)"
            )
            return

        game_id, game_name = game_info_list
        # 获取当前会话的 unified_msg_origin
        current_unified_origin = event.unified_msg_origin
        parsed_current_origin = self._parse_unified_origin(current_unified_origin)

        async with self.monitor_list_lock:
            with open(self.json2_path, "r", encoding="utf-8") as f:
                monitor_list = json.load(f)

            logger.info(f"读取 monitor_list 后: {monitor_list}")
            game_id = str(game_id)

            if game_id not in monitor_list:
                monitor_list[game_id] = {
                    "name": game_name,
                    "appid": game_id,
                    "region": region,
                    "last_price": None,
                    "original_price": None,
                    "discount": None,
                    "subscribers": [],  # 直接存储 unified_msg_origin 字符串
                }

            # 检查是否已订阅该会话
            if current_unified_origin in monitor_list[game_id]["subscribers"]:
                yield event.plain_result(
                    f"您已在当前会话订阅《{game_name}》，无需重复订阅。"
                )
            else:
                monitor_list[game_id]["subscribers"].append(current_unified_origin)

                if parsed_current_origin["message_type"] == "GroupMessage":
                    msg = f"已成功在当前群组订阅《{game_name}》，价格变动将在群内通知并 @ 您（如果会话隔离开启）。"
                else:  # FriendMessage
                    msg = f"已为您订阅《{game_name}》 (AppID: {game_id})。如果这不是您想要的游戏，请使用 /steam取消订阅 删除。"

                yield event.plain_result(msg)

            with open(self.json2_path, "w", encoding="utf-8") as f:
                json.dump(monitor_list, f, ensure_ascii=False, indent=4)

        # 订阅完成后，手动触发一次价格检查，以便尽快获取初始价格或发送首次变动通知
        await self.run_monitor_prices()

    @filter.command(
        "delsteamrmd", alias={"steam取消订阅", "steam取消订阅游戏", "steam删除订阅"}
    )
    async def steamrmdremove_command(self, event: AstrMessageEvent):
        """删除游戏监控，不再提醒"""
        # 在这里等待数据初始化完成
        await self.data_initialized.wait()
        if not self.app_dict_all:
            yield event.plain_result("游戏列表数据未加载完成或加载失败，请稍后再试。")
            return

        args = event.message_str.strip().split()[1:]
        if len(args) < 1:
            yield event.plain_result(
                "请输入游戏名，例如：/steam取消订阅 Cyberpunk 2077"
            )
            return
        elif len(args) == 1 and str.isdecimal(args[0]):
            # 输入的是 appid
            app_id = args[0]
            if int(app_id) not in self.app_dict_all_reverse:
                yield event.plain_result(f"未找到 AppID 为 {app_id} 的游戏。")
                return
            game_info_list = [app_id, self.app_dict_all_reverse[int(app_id)]]
        else:
            app_name = " ".join(args)
            yield event.plain_result(f"正在搜索 {app_name}，请稍候...")
            async with self.monitor_list_lock:
                with open(self.json2_path, "r", encoding="utf-8") as f:
                    current_monitor_list_for_search = json.load(f)
                self.app_dict_subscribed = {
                    current_monitor_list_for_search[app_id][
                        "name"
                    ]: current_monitor_list_for_search[app_id]["appid"]
                    for app_id in current_monitor_list_for_search
                }
            game_info_list = await self.get_appid_by_name(
                app_name, self.app_dict_subscribed
            )
        logger.info(f"搜索结果 game_info_list: {game_info_list}")

        if not game_info_list:
            yield event.plain_result(
                f"未找到《{app_name}》在您的订阅列表中，请检查拼写或尝试更精确的名称。"
            )
            return

        game_id, game_name = game_info_list
        current_unified_origin = event.unified_msg_origin
        game_id = str(game_id)

        async with self.monitor_list_lock:
            with open(self.json2_path, "r", encoding="utf-8") as f:
                monitor_list = json.load(f)

            logger.info(f"读取 monitor_list 后: {monitor_list}")

            if game_id not in monitor_list:
                yield event.plain_result(f"《{game_name}》未被订阅，无需取消。")
                return

            found_and_removed = False
            # 直接从 subscribers 列表中移除对应的 unified_msg_origin
            if current_unified_origin in monitor_list[game_id]["subscribers"]:
                monitor_list[game_id]["subscribers"].remove(current_unified_origin)
                found_and_removed = True
                logger.info(
                    f"会话 {current_unified_origin} 已从《{game_name}》订阅者中移除。"
                )

            if found_and_removed:
                if not monitor_list[game_id][
                    "subscribers"
                ]:  # 如果一个游戏没有任何订阅者了，则完全移除该游戏
                    del monitor_list[game_id]
                    logger.info(
                        f"游戏《{game_name}》已无任何订阅者，从监控列表中移除。"
                    )
                yield event.plain_result(
                    f"已成功将您从《{game_name}》的订阅列表中移除。"
                )
            else:
                yield event.plain_result(f"您尚未订阅《{game_name}》，无需取消订阅。")

            logger.info(f"写入 monitor_list 前: {monitor_list}")
            with open(self.json2_path, "w", encoding="utf-8") as f:
                json.dump(monitor_list, f, ensure_ascii=False, indent=4)
        self.monitor_list = monitor_list  # 更新内存中的监控列表

    @filter.command("steamrmdlist", alias={"steam订阅列表", "steam订阅游戏列表"})
    async def steamremind_list_command(self, event: AstrMessageEvent):
        """查看当前用户或群组已订阅游戏列表"""
        # 在这里等待数据初始化完成
        await self.data_initialized.wait()
        if not self.app_dict_all:
            yield event.plain_result("游戏列表数据未加载完成或加载失败，请稍后再试。")
            return

        current_unified_origin = event.unified_msg_origin

        async with self.monitor_list_lock:
            with open(self.json2_path, "r", encoding="utf-8") as f:
                monitor_list = json.load(f)

        user_or_group_monitored_games = {}
        for game_id, game_info in monitor_list.items():
            if current_unified_origin in game_info.get("subscribers", []):
                user_or_group_monitored_games[game_id] = game_info

        if not user_or_group_monitored_games:
            yield event.plain_result("暂无已订阅游戏。")
            return

        count = 0
        message_parts = [Comp.Plain(text="您已订阅的游戏列表：\n")]
        for game_id, game_info in user_or_group_monitored_games.items():
            game_name = game_info.get("name", "未知游戏")
            last_price = game_info.get("last_price", "未初始化")
            original_price = game_info.get("original_price", "N/A")
            discount = game_info.get("discount", "N/A")
            count += 1

            message_parts.append(
                Comp.Plain(text=f"{count}.《{game_name}》 (AppID: {game_id})\n")
            )
            message_parts.append(
                Comp.Plain(
                    text=f"  - 当前缓存价格：¥{last_price:.2f}\n"
                    if isinstance(last_price, (int, float))
                    else f"  - 当前缓存价格：{last_price}\n"
                )
            )
            message_parts.append(
                Comp.Plain(
                    text=f"  - 原价：¥{original_price:.2f}\n"
                    if isinstance(original_price, (int, float))
                    else f"  - 原价：{original_price}\n"
                )
            )
            message_parts.append(
                Comp.Plain(
                    text=f"  - 折扣：{discount}%\n"
                    if isinstance(discount, (int, float))
                    else f"  - 折扣：{discount}\n"
                )
            )
            message_parts.append(
                Comp.Plain(
                    text=f"  - 链接：https://store.steampowered.com/app/{game_id}\n\n"
                )
            )

        yield event.chain_result(message_parts)

    @filter.command("steamrmdrefresh", alias={"steam检查订阅", "steam刷新订阅"})
    async def steamremind_test_command(self, event: AstrMessageEvent):
        """手动检查已订阅的游戏价格是否变动"""
        # 在这里等待数据初始化完成
        await self.data_initialized.wait()
        if not self.app_dict_all:
            yield event.plain_result("游戏列表数据未加载完成或加载失败，请稍后再试。")
            return

        yield event.plain_result("正在手动检查订阅的游戏价格...")
        await self.run_monitor_prices()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steamrmdlistall", alias={"steam全局订阅列表"})
    async def steamremind_list_all_command(self, event: AstrMessageEvent):
        """
        供管理员使用，输出Steam全局订阅列表，包括游戏名和所有订阅者。
        """
        # 在这里等待数据初始化完成
        await self.data_initialized.wait()
        if not self.app_dict_all:
            yield event.plain_result("游戏列表数据未加载完成或加载失败，请稍后再试。")
            return

        async with self.monitor_list_lock:
            try:
                with open(self.json2_path, "r", encoding="utf-8") as f:
                    monitor_list = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError) as e:
                logger.error(f"监控列表文件解析失败或不存在：{e}")
                yield event.plain_result("获取全局订阅列表失败：监控数据异常或不存在。")
                return

        if not monitor_list:
            yield event.plain_result("目前没有游戏被订阅。")
            return

        message_parts = [Comp.Plain(text="Steam全局订阅列表：\n\n")]
        game_count = 0
        for game_id, game_info in monitor_list.items():
            game_count += 1
            game_name = game_info.get("name", "未知游戏")

            message_parts.append(
                Comp.Plain(
                    text=f"{game_count}. 游戏名称：《{game_name}》 (AppID: {game_id})\n"
                )
            )

            subscribers = game_info.get("subscribers", [])
            if subscribers:
                message_parts.append(Comp.Plain(text="   订阅者：\n"))
                for sub_origin in subscribers:
                    parsed_origin = self._parse_unified_origin(sub_origin)
                    if parsed_origin["message_type"] == "FriendMessage":
                        message_parts.append(
                            Comp.Plain(
                                text=f"     - 私聊用户: {parsed_origin['user_id']}\n"
                            )
                        )
                    elif parsed_origin["message_type"] == "GroupMessage":
                        group_id_str = (
                            f"群组: {parsed_origin['group_id']}"
                            if parsed_origin["group_id"]
                            else "未知群组"
                        )
                        user_id_str = (
                            f", 订阅者: {parsed_origin['user_id']}"
                            if parsed_origin["user_id"]
                            else ""
                        )
                        message_parts.append(
                            Comp.Plain(text=f"     - {group_id_str}{user_id_str}\n")
                        )
            else:
                message_parts.append(Comp.Plain(text="   无订阅者\n"))
            message_parts.append(Comp.Plain(text="\n"))  # 每个游戏之间空一行

        yield event.chain_result(message_parts)

    @filter.command("steamrmdhelp", alias={"steam订阅帮助"})
    async def steamremind_help_command(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_message = """
        Steam订阅插件帮助：
        订阅游戏：/steam订阅 [游戏名/AppID]
        取消订阅游戏：/steam取消订阅 [游戏名/AppID]
        查看订阅列表：/steam订阅列表
        手动检查订阅：/steam检查订阅
        查看全局订阅列表：/steam全局订阅列表 （需管理员权限）
        显示帮助信息：/steam订阅帮助
        """
        yield event.plain_result(help_message)

    async def terminate(self):
        if self.scheduler:
            self.scheduler.shutdown()
