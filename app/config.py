from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PDF_EDITOR_",
        extra="ignore",
    )

    data_dir: Path = Path("data")
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_upload_mb: int = 100
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
