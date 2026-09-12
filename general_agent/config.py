"""配置加载：env > .env > config.yaml > 默认值"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

DEFAULT_CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.yaml"


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 9093


class LLMSettings(BaseModel):
    # OpenAI 兼容端点；留空则指向本地 stub（:9094）
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-4o-mini"
    timeout: float = 60.0
    # 采样温度：默认 0（确定性优先，执行 LLM 与路由兜底 LLM 均生效）
    temperature: float = 0.0


class RouteRule(BaseModel):
    """规则路由条目：正则/命令前缀命中 -> 目标 Skill（按名或按 category）。"""

    pattern: str  # 正则表达式，作用于用户消息文本（search 匹配）
    skills: list[str] = []  # 目标 Skill 名集合（与 category 二选一或并存，取并集）
    category: str = ""  # 目标技能域


class RoutingSettings(BaseModel):
    """Skill 意图路由（规则 -> 向量检索 -> LLM 兜底 -> 澄清）。"""

    enabled: bool = True
    top_k: int = 20  # 向量检索返回的候选 Skill 数
    score_threshold: float = 0.5  # top-1 余弦相似度高于该值才可能高置信收窄（按 embedding 模型调优）
    margin: float = 0.1  # top-1 与 top-2 分差需大于该值才判定高置信
    rules: list[RouteRule] = []
    # 路由拼接的最近对话轮数（跨轮上下文），0=关闭拼接（澄清闭环不受影响，独立读取末条澄清行）
    context_turns: int = 3
    query_rewrite: bool = True  # 按需 query 改写开关（结合历史把指代表述改写为自包含 query）
    # 内置常用中文指代词默认表：命中则判定 query 含指代/省略，需结合历史改写
    refer_terms: list[str] = [
        "它", "他", "她", "他们", "她们", "它们",
        "这个", "那个", "这些", "那些", "这家", "那家", "这款", "那款", "这件", "那件",
        "上面", "前面", "上述", "刚才", "刚说的", "之前说的", "上一个",
        "再来", "继续", "还是", "同样", "换成",
    ]
    multi_vector: bool = True  # 每-example 多向量索引开关（示例文本逐句切分 embedding）
    llm_conf_high: float = 0.7  # 兜底 LLM 选域高置信点名阈值（≥该值直接收窄到单域）
    arg_guard: bool = True  # 缺参校验守卫开关（构造期绑定，低置信/澄清场景拦截必填参数缺失）
    hybrid: bool = True  # BM25 关键词路 + 向量路混合检索开关
    rrf_k: int = 60  # RRF 融合常数（两路排名 reciprocal rank fusion）
    keyword_top_k: int = 10  # 关键词路（BM25）返回候选数
    clarify_options: bool = True  # 低置信时给用户返回结构化澄清选项的开关
    clarify_option_max: int = 4  # 澄清选项数量上限


class EmbeddingSettings(BaseModel):
    """Embedding 端点（OpenAI 兼容 POST {base_url}/v1/embeddings）；留空指向本地 stub。"""

    base_url: str = ""
    api_key: str = ""
    model: str = "doubao-embedding-vision"  # 部署时按实际 embedding 模型 id 覆盖
    timeout: float = 30.0
    cache_dir: str = ".skill_index_cache"  # Skill 向量索引本地缓存目录
    batch_size: int = Field(default=64, ge=1)  # 索引构建时 embed_texts 分批请求的批大小


class AgentSettings(BaseModel):
    # 工具调用轮数上限；超出触发 GraphRecursionError -> turn_end{finishReason:"max_tool_rounds"}
    max_tool_rounds: int = 8
    # 上下文 token 预算，超出则 trim（token 估算用 len(content)//4 粗估，非精确 tokenizer）
    max_context_tokens: int = 24000
    context_strategy: str = "trim"  # trim | trim_then_summarize（summarize 需额外 LLM 调用，预留）
    summarize_threshold: float = 0.5  # 触发摘要的历史占比阈值（预留）
    # Agent 系统提示词；业务方可通过 config/env 覆盖
    system_prompt: str = "你是一个通用 AI 助手，可通过工具（Skill）帮助用户完成任务。需要调用工具时直接调用。工具提示缺少参数时，不要编造参数，先向用户询问缺失的信息。"


class BrokerSettings(BaseModel):
    """M7 事件中枢 Broker + 心跳配置"""
    ring_size: int = 256  # 每会话 ring buffer 容量（续传窗口）
    sub_queue_size: int = 1024  # 订阅者队列容量（背压阈值）
    heartbeat_interval: float = 15.0  # 心跳间隔，须 < 链路最短 idle 超时（网关 60s）


class RateLimitSettings(BaseModel):
    """M6 内存限流配置"""
    enabled: bool = True
    rps: float = 5.0  # 每秒补充令牌数
    burst: int = 10  # 桶容量（允许突发）


class SessionAuthSettings(BaseModel):
    """Cookie+Session 登录态配置（auth_mode=session）"""
    ttl_hours: float = 168.0  # 登录态有效期（小时），滑动续期
    cookie_secure: bool = False  # 生产 HTTPS 下设 true
    cookie_name: str = "ga_session"


class WebSettings(BaseModel):
    """Web 链路身份维度（写入消息的 service/env）"""
    service: str = "web"
    env: str = "dev"


class SecuritySettings(BaseModel):
    """M6 治理与安全"""
    auth_mode: str = "disabled"  # disabled | api_key | jwt(预留) | session(Web 登录)
    api_keys: list[str] = []  # 合法 X-Api-Key，多个 key 共用
    allowed_envs: list[str] = []  # env 白名单，空=不限制
    cors_origins: list[str] = ["*"]  # CORS 白名单；session 模式禁止 ["*"]
    session: SessionAuthSettings = SessionAuthSettings()
    web: WebSettings = WebSettings()
    rate_limit: RateLimitSettings = RateLimitSettings()


class RedisSettings(BaseModel):
    nodes: str = ""
    url: str = ""
    ssl: bool = False
    password: str = ""


class ArtifactSettings(BaseModel):
    """大产物外置存储（Tier2 blob）配置。"""
    dir: str = "artifacts"  # 本地 blob 根目录（相对路径基于服务工作目录）
    inline_threshold: int = 32 * 1024  # 内容字节数超过该值则外置到 blob，否则内联 MySQL
    head_chars: int = 2000  # 外置时行内保留的机械截断 head 字符数（不调 LLM）


class MysqlSettings(BaseModel):
    # agent 消息持久化（agent_message 表，启动时自动建表）；不启用 MySQL 时可留空默认配置
    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "general_agent"
    pool_size: int = 5


class LogSettings(BaseModel):
    level: str = "INFO"
    json_output: bool = True
    redact_secrets: bool = True  # M6：结构化日志脱敏


class OtlpSettings(BaseModel):
    endpoint: str = ""  # OTLP 端点，如 http://localhost:4318，留空则不导出 OTLP
    protocol: str = "http"  # http | grpc（默认 http/protobuf）


class TracesSettings(BaseModel):
    sampling: float = 1.0  # 1.0 = 100%（ParentBased(root=ALWAYS_ON)）


class MetricsSettings(BaseModel):
    export_interval_ms: int = 10000


class ObservabilitySettings(BaseModel):
    enabled: bool = True
    service_name: str = "general-agent"
    deployment_environment: str = "dev"  # agent 部署环境（非请求 env）
    console: bool = True  # dev 输出 console exporter，生产关闭
    otlp: OtlpSettings = OtlpSettings()
    traces: TracesSettings = TracesSettings()
    metrics: MetricsSettings = MetricsSettings()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    server: ServerSettings = ServerSettings()
    llm: LLMSettings = LLMSettings()
    routing: RoutingSettings = RoutingSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    agent: AgentSettings = AgentSettings()
    broker: BrokerSettings = BrokerSettings()
    security: SecuritySettings = SecuritySettings()
    redis: RedisSettings = RedisSettings()
    mysql: MysqlSettings = MysqlSettings()
    artifacts: ArtifactSettings = ArtifactSettings()
    log: LogSettings = LogSettings()
    observability: ObservabilitySettings = ObservabilitySettings()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_source = YamlConfigSettingsSource(
            settings_cls,
            yaml_file=str(DEFAULT_CONFIG_FILE),
            yaml_file_encoding="utf-8",
        )
        return (
            env_settings,
            dotenv_settings,
            yaml_source,
            init_settings,
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
