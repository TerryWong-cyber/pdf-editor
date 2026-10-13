from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PDF_EDITOR_",
        extra="ignore",
    )

    data_dir: Path = Path("data")
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_upload_mb: int = Field(default=100, ge=1)
    mcp_enabled: bool = True
    mcp_max_concurrency: int = Field(default=2, ge=1, le=16)
    # JSON mapping: stable principal name -> secret Bearer token.
    api_keys: dict[str, str] = Field(default_factory=dict)
    public_base_url: str = ""

    @model_validator(mode="after")
    def validate_access_settings(self):
        if any(not name.strip() or not token.strip() for name, token in self.api_keys.items()):
            raise ValueError("API identities and tokens must not be empty")
        if len(set(self.api_keys.values())) != len(self.api_keys):
            raise ValueError("each API identity must have a distinct token")
        if self.public_base_url:
            url = urlsplit(self.public_base_url)
            if (
                url.scheme not in {"http", "https"}
                or not url.netloc
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    "public_base_url must be an HTTP(S) origin with optional path prefix"
                )
        return self

    font_file: Path = Field(
        default=Path("/usr/share/fonts/opentype/noto/NotoSansSC-VF.ttf"),
        validation_alias=AliasChoices("FONT_FILE", "PDF_EDITOR_FONT_FILE"),
    )
    font_zh_file: Path = Field(
        default=Path("/usr/share/fonts/opentype/noto/NotoSansSC-VF.ttf"),
        validation_alias=AliasChoices("FONT_ZH_FILE", "PDF_EDITOR_FONT_ZH_FILE"),
    )
    font_zh_cn_file: Path = Field(
        default=Path("/usr/share/fonts/opentype/noto/NotoSansSC-VF.ttf"),
        validation_alias=AliasChoices("FONT_ZH_CN_FILE", "PDF_EDITOR_FONT_ZH_CN_FILE"),
    )
    font_zh_tw_file: Path = Field(
        default=Path("/usr/share/fonts/opentype/noto/NotoSansTC-VF.ttf"),
        validation_alias=AliasChoices("FONT_ZH_TW_FILE", "PDF_EDITOR_FONT_ZH_TW_FILE"),
    )
    font_latin_file: Path = Field(
        default=Path("/usr/share/fonts/opentype/noto/NotoSans-VF.ttf"),
        validation_alias=AliasChoices("FONT_LATIN_FILE", "PDF_EDITOR_FONT_LATIN_FILE"),
    )
    font_ja_file: Path = Field(
        default=Path("/usr/share/fonts/opentype/noto/NotoSansJP-VF.ttf"),
        validation_alias=AliasChoices("FONT_JA_FILE", "PDF_EDITOR_FONT_JA_FILE"),
    )
    font_ko_file: Path = Field(
        default=Path("/usr/share/fonts/opentype/noto/NotoSansKR-VF.ttf"),
        validation_alias=AliasChoices("FONT_KO_FILE", "PDF_EDITOR_FONT_KO_FILE"),
    )
    font_arabic_file: Path = Field(
        default=Path("/usr/share/fonts/opentype/noto/NotoSansArabic-VF.ttf"),
        validation_alias=AliasChoices("FONT_ARABIC_FILE", "PDF_EDITOR_FONT_ARABIC_FILE"),
    )
    font_cyrillic_file: Path = Field(
        default=Path("/usr/share/fonts/opentype/noto/NotoSans-VF.ttf"),
        validation_alias=AliasChoices("FONT_CYRILLIC_FILE", "PDF_EDITOR_FONT_CYRILLIC_FILE"),
    )
    font_thai_file: Path = Field(
        default=Path("/usr/share/fonts/opentype/noto/NotoSansThai-VF.ttf"),
        validation_alias=AliasChoices("FONT_THAI_FILE", "PDF_EDITOR_FONT_THAI_FILE"),
    )
    font_vietnamese_file: Path = Field(
        default=Path("/usr/share/fonts/opentype/noto/NotoSans-VF.ttf"),
        validation_alias=AliasChoices("FONT_VIETNAMESE_FILE", "PDF_EDITOR_FONT_VIETNAMESE_FILE"),
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
