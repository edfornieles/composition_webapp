from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.timezone import now 

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    mobile = models.CharField(max_length=15, blank=True, null=True)
    gender = models.CharField(max_length=10, blank=True, null=True)
    dob = models.DateField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.user.username

@receiver(post_save, sender=User)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    Profile.objects.get_or_create(user=instance)  # Ensures a Profile is always created


from django.utils.text import slugify

class Series(models.Model):
    name = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(default=now)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Composition(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]
    MOOD_RATING_CHOICES = [
        ("chill", "Chill"),
        ("mid", "Mid"),
        ("agro", "Agro"),
    ]

    name = models.CharField(max_length=255)
    type = models.CharField(max_length=100)
    date = models.DateTimeField(auto_now_add=True)
    img = models.ImageField(upload_to="composition_thumbnails/", null=True, blank=True)

    background_video = models.FileField(upload_to="videos/backgrounds/")
    foreground_video = models.FileField(upload_to="videos/foregrounds/")
    audio_file = models.FileField(upload_to="audio/", blank=True, null=True)

    final_video = models.FileField(upload_to="videos/final/", blank=True, null=True)
    url = models.URLField(max_length=500, blank=True, null=True)  # ✅ New field
    page_url = models.URLField(max_length=500, blank=True, null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    brightness = models.IntegerField(default=50)
    saturation = models.IntegerField(default=50)
    opacity = models.IntegerField(default=100)
    transition = models.CharField(max_length=20, choices=[("none", "No transition"), ("fade", "Fade"), ("crossfade", "Crossfade")], default="none")
    playback_speed = models.FloatField(default=1.0)
    source_playback_mode = models.CharField(
        max_length=20,
        choices=[("chronological", "Chronological"), ("random", "Randomize across selected sources")],
        default="random",
    )
    landscape_only = models.BooleanField(default=False)
    auto_link_delay_seconds = models.PositiveIntegerField(default=0)
    random_link_enabled = models.BooleanField(default=True)
    random_link_scope = models.CharField(
        max_length=20,
        choices=[("all", "All compositions"), ("same_series", "Same series only")],
        default="all",
    )
    series = models.ForeignKey(Series, on_delete=models.SET_NULL, null=True, blank=True, related_name="compositions")
    allowed_personas = models.ManyToManyField(
        "MonologuePersona",
        blank=True,
        related_name="allowed_compositions",
        help_text="If set, this composition is limited to the selected characters in Thoughtscape.",
    )
    filter_preset = models.CharField(max_length=50, default="none")
    filter_intensity = models.PositiveIntegerField(default=40)
    filter_param_1 = models.FloatField(default=0.5)
    filter_param_2 = models.FloatField(default=0.5)
    filter_param_3 = models.FloatField(default=0.5)
    filter_settings = models.JSONField(default=dict, blank=True)
    composition_hashtags = models.JSONField(default=list, blank=True)
    composition_emotions = models.JSONField(default=list, blank=True)
    composition_themes = models.JSONField(default=list, blank=True)
    composition_characters = models.JSONField(default=list, blank=True)
    publish_excluded_files = models.JSONField(default=list, blank=True)
    # Per-image transform overrides: {"source/file.jpg": {"flipH": true, "flipV": false, "crop": [x, y, w, h]}}
    source_image_transforms = models.JSONField(default=dict, blank=True)
    ready_for_deployment = models.BooleanField(default=False)
    mood_rating = models.CharField(max_length=10, choices=MOOD_RATING_CHOICES, default="mid")

    background_sources = models.JSONField(default=list)
    foreground_sources = models.JSONField(default=list)
    overlay_sources = models.JSONField(default=list)
    overlay_landscape_only = models.BooleanField(default=False)
    overlay_speed = models.FloatField(default=1.0)
    overlay_scale = models.FloatField(default=1.0)
    overlay_opacity = models.FloatField(default=1.0)
    overlay_rotate = models.BooleanField(default=False)

    # Optional flat-color tint layer applied either above the background (below
    # foreground/overlay) or above the overlay (above everything).
    COLOR_LAYER_TARGET_CHOICES = [
        ("background", "Above background"),
        ("overlay", "Above overlay"),
    ]
    color_layer_enabled = models.BooleanField(default=False)
    color_layer_color = models.CharField(max_length=9, default="#000000")
    color_layer_opacity = models.FloatField(default=0.3)
    color_layer_target = models.CharField(
        max_length=16, choices=COLOR_LAYER_TARGET_CHOICES, default="background"
    )

    # Text layers (Paik / Ashley TV-opera-inspired). Up to ~4 entries; each is
    # a dict with keys:
    #   text, preset, font_family, font_size_pct, color, position, z_target,
    #   start_sec, duration_sec
    # `preset` is the named effect (static / flicker / typewriter / scroll-band
    # / scroll-up / bounce / karaoke / glitch / rgb-shift / strobe / drop-in /
    # flash). The live template renders each entry with the matching CSS/JS
    # animation. JSON-only so we can add new presets without further
    # migrations.
    text_layers = models.JSONField(default=list, blank=True)
    OVERLAY_FIT_CHOICES = [
        ("free", "Free (unconstrained)"),
        ("framed", "Framed (cropped, even borders)"),
        ("square", "Square (1:1)"),
        ("portrait", "Portrait (9:16)"),
    ]
    overlay_fit = models.CharField(max_length=16, choices=OVERLAY_FIT_CHOICES, default="free")
    # Only used when overlay_fit == "framed". Even border as a fraction of each side (0.0 – 0.45).
    # e.g. 0.12 = 12% margin on every side → overlay fills the middle 76% (in both axes) of the screen.
    overlay_frame_margin = models.FloatField(default=0.12)

    NFT_MODE_CHOICES = [
        ("live", "Live (dynamic)"),
        ("epoch", "Epoch (versioned dynamic)"),
        ("frozen", "Frozen snapshot"),
    ]
    nft_enabled = models.BooleanField(default=False)
    nft_mode = models.CharField(max_length=16, choices=NFT_MODE_CHOICES, default="live")
    nft_name = models.CharField(max_length=255, blank=True, default="")
    nft_description = models.TextField(blank=True, default="")
    nft_external_url = models.URLField(max_length=500, blank=True, default="")
    nft_metadata_file = models.CharField(max_length=500, blank=True, default="")
    nft_last_generated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)

    # Folder grouping: every composition belongs to exactly one folder, identified
    # by grid_id. A new composition gets a fresh folder (= itself, cell 1). The
    # "Generate Grid" action populates siblings (cells 2..9) into that folder.
    grid_id = models.CharField(max_length=32, blank=True, default="", db_index=True)
    grid_cell_index = models.PositiveSmallIntegerField(default=0)

    def save(self, *args, **kwargs):
        if not self.grid_id:
            import uuid as _uuid
            self.grid_id = _uuid.uuid4().hex[:10]
            if not self.grid_cell_index:
                self.grid_cell_index = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class CompositionRelease(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("published", "Published"),
        ("failed", "Failed"),
    ]
    composition = models.ForeignKey(Composition, on_delete=models.CASCADE, related_name="releases")
    version_number = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    publish_mode = models.CharField(max_length=20, default="update")
    release_note = models.CharField(max_length=500, blank=True, default="")
    r2_bucket = models.CharField(max_length=255, blank=True, default="")
    r2_prefix = models.CharField(max_length=500, blank=True, default="")
    manifest = models.JSONField(default=list, blank=True)
    manifest_hash = models.CharField(max_length=128, blank=True, default="")
    uploaded_count = models.PositiveIntegerField(default=0)
    deleted_count = models.PositiveIntegerField(default=0)
    metadata_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["composition_id", "-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["composition", "version_number"],
                name="unique_composition_release_version",
            )
        ]

    def __str__(self):
        return f"{self.composition.name} release v{self.version_number}"


class CompositionReleaseFile(models.Model):
    release = models.ForeignKey(CompositionRelease, on_delete=models.CASCADE, related_name="files")
    relative_path = models.CharField(max_length=700)
    file_size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=128, blank=True, default="")
    mime_type = models.CharField(max_length=255, blank=True, default="")
    included = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=now)

    class Meta:
        ordering = ["relative_path"]
        constraints = [
            models.UniqueConstraint(
                fields=["release", "relative_path"],
                name="unique_composition_release_file_path",
            )
        ]

    def __str__(self):
        return f"{self.release_id}:{self.relative_path}"


class CompositionNFT(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("rendering", "Rendering"),
        ("ready", "Ready to mint"),
        ("minted", "Minted"),
        ("failed", "Failed"),
    ]
    CHAIN_CHOICES = [
        ("ethereum", "Ethereum"),
    ]

    composition = models.ForeignKey(Composition, on_delete=models.CASCADE, related_name="nft_versions")
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_versions",
    )
    version_number = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    chain = models.CharField(max_length=20, choices=CHAIN_CHOICES, default="ethereum")
    chain_id = models.PositiveIntegerField(default=1)
    contract_address = models.CharField(max_length=128, blank=True, default="")
    token_id = models.CharField(max_length=128, blank=True, default="")
    token_uri = models.CharField(max_length=500, blank=True, default="")
    metadata_uri = models.CharField(max_length=500, blank=True, default="")
    image_uri = models.CharField(max_length=500, blank=True, default="")
    animation_uri = models.CharField(max_length=500, blank=True, default="")
    local_image_file = models.CharField(max_length=500, blank=True, default="")
    local_video_file = models.CharField(max_length=500, blank=True, default="")
    local_collector_video_file = models.CharField(max_length=500, blank=True, default="")
    local_metadata_file = models.CharField(max_length=500, blank=True, default="")
    live_url = models.URLField(max_length=500, blank=True, default="")
    owner_wallet = models.CharField(max_length=128, blank=True, default="")
    minter_wallet = models.CharField(max_length=128, blank=True, default="")
    tx_hash = models.CharField(max_length=128, blank=True, default="")
    settings_snapshot = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default="")
    prepared_at = models.DateTimeField(null=True, blank=True)
    minted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["composition_id", "version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["composition", "version_number"],
                name="unique_composition_nft_version",
            )
        ]

    def __str__(self):
        return f"{self.composition.name} NFT v{self.version_number}"


class CompositionMediaAsset(models.Model):
    KIND_CHOICES = [
        ("poster", "Poster still"),
        ("preview_10s", "10s preview video"),
        ("collector_45s", "45s collector video"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("rendering", "Rendering"),
        ("ready", "Ready"),
        ("failed", "Failed"),
    ]

    composition = models.ForeignKey(Composition, on_delete=models.CASCADE, related_name="media_assets")
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    file = models.FileField(upload_to="nft/generated/", blank=True, null=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    aspect_preset = models.CharField(max_length=24, default="square")
    source_signature = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    error_message = models.TextField(blank=True, default="")
    generated_at = models.DateTimeField(null=True, blank=True)
    archive = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["composition_id", "kind"]
        constraints = [
            models.UniqueConstraint(
                fields=["composition", "kind"],
                name="unique_composition_media_asset_kind",
            )
        ]

    def __str__(self):
        return f"{self.composition.name} {self.kind}"

    @property
    def is_stale(self):
        from .nft_media import composition_source_signature

        return bool(self.source_signature) and self.source_signature != composition_source_signature(self.composition)


class NFTMintVoucher(models.Model):
    """
    EIP-712 lazy-mint voucher for TheFeed collection.
    The creator signs a voucher off-chain; the collector redeems it on-chain
    via mintWithVoucher(), paying at least min_price_wei to mint one token
    with the voucher's URI attached.
    """
    composition = models.ForeignKey(
        Composition, on_delete=models.CASCADE, related_name="mint_vouchers"
    )
    min_price_wei = models.BigIntegerField(help_text="Minimum ETH in wei (e.g. 70000000000000000 = 0.07 ETH)")
    uri = models.CharField(max_length=500, help_text="Metadata URI embedded in the voucher (e.g. ipfs://...)")
    nonce = models.BigIntegerField(unique=True, help_text="Unique nonce to prevent replay")
    signature = models.CharField(max_length=200, help_text="0x-prefixed EIP-712 signature from authorizedSigner")
    signed_by = models.CharField(max_length=42, help_text="Ethereum address that signed this voucher")
    signed_at = models.DateTimeField(null=True, blank=True)
    redeemed = models.BooleanField(default=False)
    redeemed_tx = models.CharField(max_length=128, blank=True, default="", help_text="Transaction hash when redeemed")
    redeemed_token_id = models.CharField(max_length=64, blank=True, default="")
    redeemed_by = models.CharField(max_length=42, blank=True, default="", help_text="Collector wallet address")
    redeemed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        status = "redeemed" if self.redeemed else "active"
        return f"Voucher #{self.pk} for {self.composition.name} ({status})"

    @property
    def min_price_eth(self):
        return self.min_price_wei / 1e18


class MintCollectionSettings(models.Model):
    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    collection_title = models.CharField(max_length=255, default="Aggregation Works")
    collection_icon = models.ImageField(upload_to="mint_collection/", blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Mint collection settings"
        verbose_name_plural = "Mint collection settings"

    def __str__(self):
        return self.collection_title

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(singleton_key=1)
        return obj

    @property
    def icon_url(self):
        if not self.collection_icon:
            return ""
        try:
            return self.collection_icon.url
        except ValueError:
            return ""


class MonologuePersona(models.Model):
    """
    Text-only “stream of consciousness” page for a character.
    Curate style_corpus / stream_seed_text from public posts (respect ToS & attribution).
    Optional source_urls documents inspiration links; ingestion/LLM is not wired in MVP.
    """

    slug = models.SlugField(max_length=120, unique=True, db_index=True)
    title = models.CharField(max_length=255)
    character_name = models.CharField(max_length=255, blank=True, default="")
    archetype = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Label for tone (e.g. jock, poet). Used for fallback lines if no corpus is set.",
    )
    source_urls = models.JSONField(
        default=list,
        blank=True,
        help_text="List of reference URLs (social, blogs, news) for future ingestion / RAG.",
    )
    style_corpus = models.TextField(
        blank=True,
        default="",
        help_text="Pasted excerpts or notes that shape voice (your curated training text).",
    )
    stream_seed_text = models.TextField(
        blank=True,
        default="",
        help_text="Optional full script: paragraphs separated by blank lines become segments.",
    )
    system_prompt_hint = models.TextField(
        blank=True,
        default="",
        help_text="For future LLM: system prompt fragment describing the character.",
    )
    typing_min_ms = models.PositiveIntegerField(default=28)
    typing_max_ms = models.PositiveIntegerField(default=85)
    pause_between_segments_ms = models.PositiveIntegerField(default=1400)
    is_published = models.BooleanField(default=False)
    cached_visual_urls = models.JSONField(
        default=list,
        blank=True,
        help_text="Image URLs (e.g. og:image) harvested from source_urls for the public page backdrop.",
    )
    visual_cache_updated_at = models.DateTimeField(null=True, blank=True)

    base_model = models.CharField(
        max_length=255, blank=True, default="",
        help_text="HuggingFace ID for the MLX base, e.g. mlx-community/Qwen2.5-7B-Instruct-4bit.",
    )
    adapter_dir = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Filesystem path to the trained LoRA adapter directory.",
    )
    corpus_key = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Imageboard corpus key, e.g. fourchan_fit.",
    )
    image_dir = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Filesystem directory for this character's per-character image pool.",
    )
    persona_prompt = models.TextField(
        blank=True, default="",
        help_text="System prompt that sculpts the character's voice for chat + thoughts.",
    )
    gen_temperature = models.FloatField(default=0.95)
    gen_top_p = models.FloatField(default=0.95)
    gen_max_tokens = models.PositiveIntegerField(default=140)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title

    @property
    def has_trained_model(self) -> bool:
        from pathlib import Path
        if not self.adapter_dir:
            return False
        try:
            return Path(self.adapter_dir).exists()
        except Exception:
            return False


class ChatSession(models.Model):
    """Persistent studio chat session with one character.

    `state` holds Pass-1 chat-continuity scaffolding:
      - anon_variant       : key into imageboard_ingestion.anon_zoo
      - op_anchor          : first user message captured at session start
      - session_summary    : compressed memory beyond the rolling-history cliff
      - last_summary_turn  : turn index when the summary was last regenerated
      - recent_fragments   : list of fragment chunk_ids served in last N turns
                             (for retrieval anti-repeat)
    Older rows have empty state; callers must lazily initialise on first
    generation.
    """
    persona = models.ForeignKey(
        MonologuePersona, related_name="chat_sessions", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=120, default="new chat")
    state = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.persona.slug}/{self.name}"


class ChatMessage(models.Model):
    ROLE_CHOICES = (("user", "user"), ("anon", "anon"))
    session = models.ForeignKey(
        ChatSession, related_name="messages", on_delete=models.CASCADE
    )
    role = models.CharField(max_length=8, choices=ROLE_CHOICES)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class FavoriteThought(models.Model):
    """Saved/curated generated thought; used as gallery fallback pool."""
    persona = models.ForeignKey(
        MonologuePersona, related_name="favorite_thoughts", on_delete=models.CASCADE
    )
    text = models.TextField()
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class AdvancedThoughtScenario(models.Model):
    slug = models.SlugField(max_length=120, unique=True, db_index=True)
    title = models.CharField(max_length=255)
    character_name = models.CharField(max_length=255, blank=True, default="")
    archetype = models.CharField(max_length=80, blank=True, default="")
    character_brief = models.TextField(blank=True, default="")
    world_context = models.TextField(blank=True, default="")
    tone_hint = models.TextField(blank=True, default="")
    seed_desires = models.JSONField(default=list, blank=True)
    seed_subconscious = models.JSONField(default=list, blank=True)
    scene_bible = models.JSONField(
        default=dict,
        blank=True,
        help_text="Stable environment anchors (room objects, recurring visual motifs).",
    )
    relationship_graph = models.JSONField(
        default=list,
        blank=True,
        help_text="Key people and relationship dynamics for social continuity.",
    )
    is_published = models.BooleanField(default=False)
    tick_interval_seconds = models.PositiveIntegerField(default=7)
    sim_minutes = models.PositiveIntegerField(default=8 * 60)
    current_location = models.CharField(max_length=255, blank=True, default="bedroom")
    continuity_note = models.TextField(blank=True, default="")
    last_thought = models.TextField(blank=True, default="")
    last_image_url = models.URLField(max_length=1000, blank=True, default="")
    last_visual_prompt = models.TextField(blank=True, default="")
    last_desires = models.JSONField(default=list, blank=True)
    last_subconscious = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class WallProfile(models.Model):
    name = models.CharField(max_length=120, unique=True)
    rows = models.PositiveIntegerField(default=3)
    cols = models.PositiveIntegerField(default=3)
    strict_live = models.BooleanField(default=True)
    cadence_seconds = models.PositiveIntegerField(default=12)
    start_lead_ms = models.PositiveIntegerField(default=2000)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class WallRun(models.Model):
    STATUS_CHOICES = [
        ("stopped", "Stopped"),
        ("running", "Running"),
        ("paused", "Paused"),
    ]
    profile = models.ForeignKey(WallProfile, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="stopped")
    tick = models.PositiveIntegerField(default=0)
    current_tick_id = models.PositiveIntegerField(default=0)
    current_start_at_unix_ms = models.BigIntegerField(default=0)
    current_duration_ms = models.PositiveIntegerField(default=12000)
    active_character_keys = models.JSONField(default=list, blank=True)
    thought_text = models.TextField(blank=True, default="")
    scenario_state = models.CharField(max_length=40, blank=True, default="neutral")
    scene_location = models.CharField(max_length=255, blank=True, default="")
    scene_people = models.JSONField(default=list, blank=True)
    pool_composition_ids = models.JSONField(default=list, blank=True)
    pinned_assignments = models.JSONField(default=dict, blank=True)
    previous_assignments = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.profile.name} ({self.status})"


class WallAssignment(models.Model):
    run = models.ForeignKey(WallRun, on_delete=models.CASCADE, related_name="assignments")
    tick_id = models.PositiveIntegerField()
    tile_index = models.PositiveIntegerField()
    layer = models.CharField(max_length=24, blank=True, default="transitional")
    composition = models.ForeignKey(
        "Composition",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wall_assignments",
    )
    composition_url = models.URLField(max_length=1000, blank=True, default="")
    reason = models.CharField(max_length=120, blank=True, default="")
    start_at_unix_ms = models.BigIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=12000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "tick_id", "tile_index"], name="wall_assignment_unique_tick_tile"
            )
        ]
        ordering = ["tick_id", "tile_index"]

    def __str__(self):
        return f"Run {self.run_id} tick {self.tick_id} tile {self.tile_index}"


class WallHeartbeat(models.Model):
    run = models.ForeignKey(WallRun, on_delete=models.CASCADE, related_name="heartbeats")
    screen_id = models.CharField(max_length=40)
    last_seen = models.DateTimeField(auto_now=True)
    clock_offset_ms = models.IntegerField(default=0)
    last_applied_tick = models.IntegerField(default=0)
    drift_ms = models.IntegerField(default=0)
    last_error = models.CharField(max_length=255, blank=True, default="")
    last_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "screen_id"], name="wall_heartbeat_unique_screen")
        ]
        ordering = ["screen_id"]

    def __str__(self):
        return f"Run {self.run_id} screen {self.screen_id}"


class S3Bucket(models.Model):
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name
    

class Bucket(models.Model):
    name = models.CharField(max_length=255)
    type = models.CharField(max_length=100)
    source_id = models.CharField(max_length=255)
    last_scraped = models.DateField()
    query_no = models.BigIntegerField()
    initial = models.IntegerField()
    max_num = models.IntegerField()
    
    def __str__(self):
        return self.name

class VideoComposition(models.Model):
    audio = models.FileField(upload_to="audios/", null=True, blank=True)
    output_video = models.FileField(upload_to="videos/", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Video Composition {self.id}"

class BackgroundImage(models.Model):
    video = models.ForeignKey(VideoComposition, on_delete=models.CASCADE, related_name="backgrounds")
    image = models.ImageField(upload_to="backgrounds/")

class ForegroundImage(models.Model):
    video = models.ForeignKey(VideoComposition, on_delete=models.CASCADE, related_name="foregrounds")
    image = models.ImageField(upload_to="foregrounds/")


class IngestionBatch(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("review", "Review"),
        ("published", "Published"),
    ]
    MOOD_RATING_CHOICES = [
        ("chill", "Chill"),
        ("mid", "Mid"),
        ("agro", "Agro"),
    ]
    title = models.CharField(max_length=255, blank=True, default="")
    source_kind = models.CharField(max_length=30, default="upload")
    destination_mode = models.CharField(max_length=20, default="new")
    destination_source_name = models.CharField(max_length=255, blank=True, default="")
    mood_rating = models.CharField(max_length=10, choices=MOOD_RATING_CHOICES, default="mid")
    concept_brief = models.TextField(blank=True, default="")
    target_asset_count = models.PositiveIntegerField(default=800)
    ai_query_pack = models.JSONField(default=list, blank=True)
    campaign_enabled = models.BooleanField(default=False)
    campaign_mode = models.CharField(max_length=20, default="scheduled")
    campaign_target_count = models.PositiveIntegerField(default=800)
    campaign_cadence_minutes = models.PositiveIntegerField(default=120)
    campaign_max_pages_per_run = models.PositiveIntegerField(default=4)
    campaign_max_candidates_per_run = models.PositiveIntegerField(default=180)
    campaign_max_imports_per_run = models.PositiveIntegerField(default=90)
    campaign_runs_count = models.PositiveIntegerField(default=0)
    campaign_last_run_at = models.DateTimeField(null=True, blank=True)
    campaign_next_run_at = models.DateTimeField(null=True, blank=True)
    campaign_last_status = models.CharField(max_length=24, blank=True, default="")
    campaign_last_report = models.TextField(blank=True, default="")
    campaign_state = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    last_scrape_engine_summary = models.CharField(max_length=255, blank=True, default="")
    last_scrape_report = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=now)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title or f"Ingestion #{self.id}"


class IngestionItem(models.Model):
    DECISION_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("rejected", "Rejected"),
    ]
    batch = models.ForeignKey(IngestionBatch, on_delete=models.CASCADE, related_name="items")
    original_url = models.TextField(blank=True, default="")
    file_path = models.CharField(max_length=600)
    media_kind = models.CharField(max_length=10, default="image")
    decision = models.CharField(max_length=20, choices=DECISION_CHOICES, default="pending")
    sha256 = models.CharField(max_length=64, blank=True, default="")
    phash = models.CharField(max_length=16, blank=True, default="")
    # Base64-packed float16 CLIP ViT-B/32 embedding (512 dims → ~1.4 KB of text).
    # Stored inline on the row so dedupe can run a pure-SQL fetch and an in-memory
    # cosine scan without loading the pixels. Empty when CLIP isn't available or the
    # item is a video. See views._clip_embed_path / _clip_embed_decode.
    clip_embedding = models.TextField(blank=True, default="")
    dedupe_note = models.CharField(max_length=255, blank=True, default="")
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    orientation = models.CharField(max_length=16, blank=True, default="unknown")
    color_mode = models.CharField(max_length=16, blank=True, default="unknown")
    dominant_color_hex = models.CharField(max_length=7, blank=True, default="")
    created_at = models.DateTimeField(default=now)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"Item {self.id} ({self.media_kind})"


class SocialSimWorld(models.Model):
    slug = models.SlugField(max_length=120, unique=True, db_index=True)
    title = models.CharField(max_length=255)
    timezone = models.CharField(max_length=64, default="America/Los_Angeles")
    sim_day = models.PositiveIntegerField(default=1)
    sim_minute = models.PositiveIntegerField(default=8 * 60)
    is_active = models.BooleanField(default=True)
    weather_state = models.JSONField(default=dict, blank=True)
    rhythm_state = models.JSONField(default=dict, blank=True)
    render_policy = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class SocialLocation(models.Model):
    world = models.ForeignKey(SocialSimWorld, on_delete=models.CASCADE, related_name="locations")
    slug = models.SlugField(max_length=120, db_index=True)
    title = models.CharField(max_length=255)
    location_type = models.CharField(max_length=80, default="interior")
    x = models.FloatField(default=0.0)
    y = models.FloatField(default=0.0)
    adjacent_slugs = models.JSONField(default=list, blank=True)
    access_rules = models.JSONField(default=dict, blank=True)
    ambient_tags = models.JSONField(default=list, blank=True)

    class Meta:
        unique_together = [("world", "slug")]
        ordering = ["title"]

    def __str__(self):
        return f"{self.world.slug}:{self.slug}"


class SocialAgent(models.Model):
    world = models.ForeignKey(SocialSimWorld, on_delete=models.CASCADE, related_name="agents")
    slug = models.SlugField(max_length=120, db_index=True)
    name = models.CharField(max_length=255)
    identity_packet = models.JSONField(default=dict, blank=True)
    body_state = models.JSONField(default=dict, blank=True)
    mood_state = models.JSONField(default=dict, blank=True)
    goals_state = models.JSONField(default=list, blank=True)
    subconscious_state = models.JSONField(default=list, blank=True)
    working_self = models.TextField(blank=True, default="")
    routine_profile = models.JSONField(default=dict, blank=True)
    tendency_profile = models.JSONField(default=dict, blank=True)
    current_location = models.ForeignKey(
        SocialLocation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="present_agents",
    )
    last_plan = models.TextField(blank=True, default="")
    last_thought = models.TextField(blank=True, default="")
    last_subconscious = models.TextField(blank=True, default="")
    last_rendered_at = models.DateTimeField(null=True, blank=True)
    render_priority = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("world", "slug")]
        ordering = ["name"]

    def __str__(self):
        return f"{self.world.slug}:{self.name}"


class SocialRelationship(models.Model):
    world = models.ForeignKey(SocialSimWorld, on_delete=models.CASCADE, related_name="relationships")
    source = models.ForeignKey(SocialAgent, on_delete=models.CASCADE, related_name="outbound_relationships")
    target = models.ForeignKey(SocialAgent, on_delete=models.CASCADE, related_name="inbound_relationships")
    metrics = models.JSONField(default=dict, blank=True)
    unresolved_tensions = models.JSONField(default=list, blank=True)
    shared_history = models.TextField(blank=True, default="")
    summary = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("world", "source", "target")]

    def __str__(self):
        return f"{self.source.slug}->{self.target.slug}"


class SocialEvent(models.Model):
    world = models.ForeignKey(SocialSimWorld, on_delete=models.CASCADE, related_name="events")
    day_index = models.PositiveIntegerField(default=1)
    minute_index = models.PositiveIntegerField(default=0)
    event_type = models.CharField(max_length=80, default="ambient")
    location = models.ForeignKey(SocialLocation, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    actor_slugs = models.JSONField(default=list, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    significance = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-day_index", "-minute_index", "-id"]


class SocialMemory(models.Model):
    world = models.ForeignKey(SocialSimWorld, on_delete=models.CASCADE, related_name="memories")
    agent = models.ForeignKey(SocialAgent, on_delete=models.CASCADE, related_name="memories")
    event = models.ForeignKey(SocialEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name="memories")
    objective_summary = models.TextField(blank=True, default="")
    subjective_summary = models.TextField(blank=True, default="")
    emotional_weight = models.FloatField(default=0.0)
    distortion = models.FloatField(default=0.0)
    unresolved = models.JSONField(default=list, blank=True)
    consolidated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


# -- Masks ----------------------------------------------------------------

class Tribe(models.Model):
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    palette_json = models.JSONField(default=dict, blank=True)
    lore_md = models.TextField(blank=True, default="")
    mint_authority_pubkey = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(default=now)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class MaskAsset(models.Model):
    SLOT_CHOICES = [
        ("face", "Face"),
        ("hat", "Hat"),
        ("clothing", "Clothing"),
        ("accessory", "Accessory"),
    ]
    slot = models.CharField(max_length=16, choices=SLOT_CHOICES)
    tier = models.PositiveSmallIntegerField(default=0)
    image = models.ImageField(upload_to="mask_assets/")
    tribe = models.ForeignKey(Tribe, on_delete=models.SET_NULL, null=True, blank=True, related_name="assets")
    name = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=now)

    class Meta:
        ordering = ["slot", "tier", "name"]
        indexes = [models.Index(fields=["slot", "tier", "tribe"])]

    def __str__(self):
        return f"{self.slot}:{self.name}(t{self.tier})"


class Mask(models.Model):
    owner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="masks")
    tribe = models.ForeignKey(Tribe, on_delete=models.PROTECT, related_name="masks")
    name = models.CharField(max_length=128)
    slug = models.SlugField(max_length=80, unique=True)

    face = models.ForeignKey(MaskAsset, on_delete=models.PROTECT, related_name="masks_as_face", limit_choices_to={"slot": "face"})
    hat = models.ForeignKey(MaskAsset, on_delete=models.SET_NULL, null=True, blank=True, related_name="masks_as_hat", limit_choices_to={"slot": "hat"})
    clothing = models.ForeignKey(MaskAsset, on_delete=models.SET_NULL, null=True, blank=True, related_name="masks_as_clothing", limit_choices_to={"slot": "clothing"})
    accessory = models.ForeignKey(MaskAsset, on_delete=models.SET_NULL, null=True, blank=True, related_name="masks_as_accessory", limit_choices_to={"slot": "accessory"})

    background_source_name = models.CharField(max_length=255, blank=True, default="")

    power = models.PositiveSmallIntegerField(default=5)
    speed = models.PositiveSmallIntegerField(default=5)
    wit = models.PositiveSmallIntegerField(default=5)
    charm = models.PositiveSmallIntegerField(default=5)

    fun_level = models.IntegerField(default=0)
    mint_address = models.CharField(max_length=64, blank=True, null=True, unique=True)

    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name

    @property
    def face_tier(self):
        if self.fun_level >= 40:
            return 3
        if self.fun_level >= 15:
            return 2
        if self.fun_level >= 5:
            return 1
        return 0


class MaskBattle(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("escrowed", "Escrowed"),
        ("resolved", "Resolved"),
        ("settled", "Settled"),
        ("cancelled", "Cancelled"),
    ]
    SOURCE_CHOICES = [
        ("stats", "Stat-based roll"),
        ("polymarket", "Polymarket market"),
    ]
    resolution_source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="stats")
    polymarket_market_id = models.CharField(max_length=128, blank=True, default="")
    polymarket_attacker_outcome = models.CharField(max_length=64, blank=True, default="")
    attacker = models.ForeignKey(Mask, on_delete=models.CASCADE, related_name="battles_as_attacker")
    defender = models.ForeignKey(Mask, on_delete=models.CASCADE, related_name="battles_as_defender")
    winner = models.ForeignKey(Mask, on_delete=models.SET_NULL, null=True, blank=True, related_name="battles_won")

    wager_lamports = models.BigIntegerField(default=0)
    wager_mint = models.CharField(max_length=64, blank=True, default="")
    nft_at_stake = models.BooleanField(default=False)
    escrow_account = models.CharField(max_length=64, blank=True, default="")

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    result_signature = models.CharField(max_length=128, blank=True, default="")
    seed = models.CharField(max_length=64, blank=True, default="")
    roll_log = models.JSONField(default=dict, blank=True)

    started_at = models.DateTimeField(default=now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Battle#{self.id}: {self.attacker_id} vs {self.defender_id} ({self.status})"
