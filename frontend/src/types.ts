export type KycStatus = 'kyc' | 'non_kyc' | 'mixed' | null;
export type PathMode = 'buy' | 'sell';

export type CrawlRun = {
  id: number;
  trigger: string;
  status: string;
  message: string | null;
  usd_krw_rate?: number | null;
  started_at: number | null;
  completed_at: number | null;
};

export type TickerRow = {
  exchange: string;
  pair: string;
  market_type: string;
  currency: string;
  price: number;
  high_24h?: number | null;
  low_24h?: number | null;
  volume_24h_btc?: number | null;
  maker_fee_pct?: number | null;
  taker_fee_pct?: number | null;
  maker_fee_usd?: number | null;
  maker_fee_krw?: number | null;
  taker_fee_usd?: number | null;
  taker_fee_krw?: number | null;
  usd_krw_rate?: number | null;
};

export type WithdrawalRow = {
  exchange: string;
  coin: string;
  source: string;
  source_url?: string | null;
  network_label: string;
  fee?: number | null;
  fee_usd?: number | null;
  fee_krw?: number | null;
  enabled: boolean;
  note?: string | null;
  kyc_status?: KycStatus;
  recorded_at?: number | null;
};

export type CrawlErrorRow = {
  exchange?: string | null;
  coin?: string | null;
  stage: string;
  error_message: string;
  created_at?: string | null;
};

export type SuspendedNetwork = {
  coin?: string | null;
  network?: string | null;
  status: string;
  reason?: string | null;
  source_url?: string | null;
  detected_at?: string | null;
};

export type NetworkStatusMap = Record<string, {
  status: string;
  suspended_networks: SuspendedNetwork[];
  checked_at?: number | null;
}>;

export type CheapestPathFeeComponent = {
  label: string;
  amount_krw: number;
  rate_pct?: number | null;
  amount_text?: string | null;
  source_url?: string | null;
  is_fixed?: boolean | null;
  // 이 단계에서 옮긴 본체 수량 + 원화 환산 (결과 페이지 단계별 표시용)
  move_amount?: number | null;
  move_coin?: string | null;
  move_amount_krw?: number | null;
  // 출금 컴포넌트의 출금 네트워크 (예: TRC20, Bitcoin, Lightning)
  network?: string | null;
  // 이 수수료에 붙는 조건 안내 (예: 바우처 발급 시 무료) — 있을 때만 배지로 노출
  note?: string | null;
};

export type CheapestPathBreakdown = {
  components: CheapestPathFeeComponent[];
  total_fee_krw: number;
};

export type WalletFeeEstimate = {
  source: string;
  source_url: string;
  fee_target: 'medium';
  medium_fee_rate_sat_vb: number;
  fastest_fee_sat_vb?: number | null;
  hour_fee_sat_vb?: number | null;
  economy_fee_sat_vb?: number | null;
  minimum_fee_sat_vb?: number | null;
  address_type: 'p2wpkh';
  utxo_count: number;
  output_count: number;
  estimated_tx_vbytes: number;
  fee_sats: number;
  fee_btc: number;
  fee_krw: number;
};

export type CheapestPathEntry = {
  path_id: string;
  route_variant?: 'btc_direct' | 'btc_via_global' | 'usdt_via_global' | 'lightning_direct' | 'lightning_via_global';
  korean_exchange: string;
  transfer_coin: string;
  network: string;
  domestic_withdrawal_network: string;
  global_exit_mode: 'onchain' | 'lightning';
  global_exit_network: string;
  lightning_exit_provider?: string | null;
  path_type?: 'lightning_exit' | null;
  // 종착지: 개인 온체인 지갑(personal) / 라이트닝 지갑(lightning_wallet, LN 직접출금 종착)
  destination?: 'personal' | 'lightning_wallet';
  swap_service?: string | null;
  btc_received?: number;
  btc_received_usd?: number | null;
  krw_received?: number;
  discarded_krw?: number | null;  // 최소주문 단위로 못 쓰고 남는 잔돈(원, 근사)
  total_fee_krw: number;
  fee_pct: number;
  lightning_swap_fee_krw?: number | null;
  global_withdrawal_fee_krw?: number | null;
  num_withdrawal_txs?: number | null;
  krw_per_tx_limit?: number | null;
  breakdown?: CheapestPathBreakdown | null;
  domestic_kyc_status?: KycStatus;
  global_kyc_status?: KycStatus;
  exit_service_kyc_status?: KycStatus;
  wallet_kyc_status?: KycStatus;
  disabled?: boolean;
  disabled_reason?: string | null;
  suspension_message?: string | null;
  notice_url?: string | null;
  notice_published_at?: string | number | null;
  notice_title?: string | null;
  usd_krw_rate?: number | null;
};

export type DisabledCheapestPathEntry = {
  korean_exchange: string;
  transfer_coin: string;
  network: string;
  reason?: string | null;
  suspension_message?: string | null;
  notice_url?: string | null;
  notice_published_at?: string | number | null;
  notice_title?: string | null;
};

export type CheapestPathResponse = {
  mode: PathMode;
  amount_krw?: number;
  amount_btc?: number;
  wallet_fee_estimate?: WalletFeeEstimate | null;
  global_exchange: string;
  global_btc_price_usd: number;
  usd_krw_rate: number;
  /** USDT 매수에 실제 사용한 한국 USDT/KRW 환율 (USDT 경로 글로벌 시세 평가 기준) */
  usdt_buy_krw_rate?: number | null;
  total_paths_evaluated: number;
  best_path: CheapestPathEntry | null;
  top5: CheapestPathEntry[];
  all_paths: CheapestPathEntry[];
  disabled_paths: DisabledCheapestPathEntry[];
  available_filters: {
    domestic_withdrawal_networks: string[];
    global_exit_options: Array<{ mode: 'onchain' | 'lightning'; network: string }>;
    lightning_exit_providers: string[];
  };
  maintenance_checked_at?: number | null;
  data_source?: string;
  latest_scraping_time?: number | null;
  last_run?: CrawlRun | null;
  lightning_swap_services?: string[];
  errors?: CrawlErrorRow[];
  error?: string;
};

export type AccessStats = {
  visitors_total: number;
  visitors_today: number;
  routes_total: number;
  routes_today: number;
};

export type NetworkChangeNotice = {
  title: string;
  url: string | null;
  published_at: number | null;
};

export type NetworkChange = {
  exchange: string;
  coin: string | null;
  network: string | null;
  change_type: 'suspended' | 'resumed';
  detected_at: number | null;
  related_notices: NetworkChangeNotice[];
};

export type NetworkChangesResponse = {
  items: NetworkChange[];
};

export type LiveKimpResponse = {
  /** 비트코인 자체 프리미엄 % = 한국 BTC(KRW) ÷ (글로벌 BTC(USD) × 업비트 USDT 실거래가) − 1 */
  kimp: Record<string, number>;
  /** 김치 프리미엄(총) % = 한국 BTC(KRW) ÷ (글로벌 BTC × 두나무 포렉스) − 1 = (1+비트코인)(1+테더)−1. 포렉스 없으면 빈 객체. */
  kimchi_premium_total?: Record<string, number>;
  korean_btc_prices: Record<string, number>;
  global_btc_price_krw: number;
  usd_krw_rate: number;
  /** 두나무 원달러 포렉스 환율 (원달러 프리미엄 계산 기준) */
  forex_usd_krw_rate?: number | null;
  /** 원달러(테더) 프리미엄 % = 업비트 USDT ÷ 두나무 포렉스 − 1 */
  usdt_premium?: number | null;
  fetched_at: number;
};

export type ScrapedPageStatus = {
  label: string;
  url: string;
  category: 'network_status' | 'withdrawal' | 'lightning';
  status: 'ok' | 'error';
  last_crawled_at: number | null;
  error_message: string | null;
};

export type ScrapeStatusResponse = {
  last_run: { id: number; status: string; completed_at: number | null } | null;
  items: ScrapedPageStatus[];
};

export type LightningSwapFeeRow = {
  service_name: string;
  fee_pct: number | null;
  fee_fixed_sat: number | null;
  min_amount_sat: number | null;
  max_amount_sat: number | null;
  enabled: boolean;
  source_url: string | null;
  error_message: string | null;
  recorded_at: number | null;
};

export type ExchangeStatusWithdrawalRow = {
  coin: string;
  network_label: string;
  fee?: number | null;
  fee_krw?: number | null;
  fee_pct?: number | null;
  fee_fixed_sat?: number | null;
  min_amount_sat?: number | null;
  max_amount_sat?: number | null;
  min_withdrawal?: number | null;
  max_withdrawal?: number | null;
  enabled: boolean;
  source: string;
  note?: string | null;
  kyc_status?: KycStatus;
};

export type ExchangeStatusNode = {
  exchange: string;
  type: 'exchange' | 'lightning';
  direction?: 'onchain_to_ln' | 'ln_to_onchain' | null;
  withdrawal_rows: ExchangeStatusWithdrawalRow[];
  network_status: {
    status: string;
    suspended_networks: SuspendedNetwork[];
    checked_at?: number | null;
  };
  scrape_status: {
    url: string;
    status: 'ok' | 'error';
    last_crawled_at: number | null;
    error_message: string | null;
  } | null;
  kyc_status?: KycStatus;
  notices: Array<{
    title: string;
    url: string | null;
    published_at: number | null;
  }>;
};

export type ExchangeStatusResponse = {
  exchanges: ExchangeStatusNode[];
  lightning_services: ExchangeStatusNode[];
  latest_notices: ExchangeNoticeItem[];
};

export type ExchangeNoticeItem = {
  exchange: string;
  title: string;
  url: string | null;
  published_at: number | null;
  noticed_at: number | null;
};

export type ExchangeCapabilityRow = {
  exchange: string;
  supports_lightning_deposit: boolean;
  supports_lightning_withdrawal: boolean;
};

// /market/carf-exchanges 실제 응답 (carf_seed.py 기반)
export type CarfExchangeInfo = {
  id: string;
  name: string | null;
  shortName: string | null;
  type: string | null;
  registeredCountry: string | null;
  carfGroup: string | null;
  carfDataCollectionStart: string | null;
  carfFirstExchange: string | null;
  koreaService: string | null;
  koreaBlocked: boolean | null;
  koreaImpact: string | null;
  impactDetail: string | null;
  travelRuleKorea: string | null;
  travelRuleNote: string | null;
  koreaUserJurisdiction: string | null;
  koreaUserJurisdictionNote: string | null;
  mapLocation: unknown;
  sources: string[] | null;
};

// ── 게시판 ──────────────────────────────────────────────────────────────────
export type BoardCategory = 'general' | 'report' | 'notice';

export type BoardPostBrief = {
  id: number;
  category: BoardCategory;
  title: string;
  content_preview: string;
  nickname: string;
  comment_count: number;
  created_at: number | null;
  updated_at: number | null;
};

export type BoardComment = {
  id: number;
  post_id: number;
  nickname: string;
  content: string;
  created_at: number | null;
  updated_at: number | null;
};

export type BoardPostDetail = {
  id: number;
  category: BoardCategory;
  title: string;
  content: string;
  nickname: string;
  created_at: number | null;
  updated_at: number | null;
  comments: BoardComment[];
};

export type BoardListResponse = {
  notices: BoardPostBrief[];
  items: BoardPostBrief[];
  total: number;
  page: number;
  size: number;
};
