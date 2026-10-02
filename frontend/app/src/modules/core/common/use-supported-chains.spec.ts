import process from 'node:process';
import { server } from '@test/setup-files/server';
import { createCustomPinia } from '@test/utils/create-pinia';
import { http, HttpResponse } from 'msw';
import { setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import { useSupportedChains } from '@/modules/core/common/use-supported-chains';

describe('useSupportedChains', () => {
  beforeEach(() => {
    setActivePinia(createCustomPinia());
  });

  describe('isEarlyIntegrationChain', () => {
    it('should return true for chains with limited protocol coverage', () => {
      const { isEarlyIntegrationChain } = useSupportedChains();
      expect(isEarlyIntegrationChain('avax')).toBe(true);
      expect(isEarlyIntegrationChain('hyperliquid')).toBe(true);
      expect(isEarlyIntegrationChain('monad')).toBe(true);
    });

    it('should return false for fully supported or unknown chains', () => {
      const { isEarlyIntegrationChain } = useSupportedChains();
      expect(isEarlyIntegrationChain('eth')).toBe(false);
      expect(isEarlyIntegrationChain('ethereum')).toBe(false);
      expect(isEarlyIntegrationChain('solana')).toBe(false);
      expect(isEarlyIntegrationChain('')).toBe(false);
    });
  });

  /*
   * TRON is neither EVM nor Solana: its accounts live on their own page, and its native asset is
   * TRX, which the account rows read the native balance from. A basic chain entry would drop the
   * native token and fall back to the chain id.
   */
  describe('tron', () => {
    beforeEach(async () => {
      server.use(http.get(`${process.env.VITE_BACKEND_URL}/api/1/blockchains/supported`, () => HttpResponse.json({
        result: [{ id: 'tron', image: 'tron.svg', name: 'TRON', native_token: 'TRX', type: 'tron' }],
      })));
      await useSupportedChains().refreshSupportedChains();
    });

    it('should keep TRX as the native asset', () => {
      expect(useSupportedChains().getNativeAsset('tron')).toBe('TRX');
    });

    it('should send TRON to its own accounts page, not the EVM one', () => {
      const { getBlockchainRedirectLink, isEvm, isSolanaChains } = useSupportedChains();
      expect(getBlockchainRedirectLink('tron')).toBe('/accounts/tron?chain=tron');
      expect(isEvm('tron')).toBe(false);
      expect(isSolanaChains('tron')).toBe(false);
    });
  });
});
