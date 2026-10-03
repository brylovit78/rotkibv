import { HistoryEventEntryType } from '@rotki/common';
import { createMock } from '@test/utils/create-mock';
import { libraryDefaults } from '@test/utils/provide-defaults';
import { mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, describe, expect, it } from 'vitest';
import HistoryEventAction from '@/modules/history/events/HistoryEventAction.vue';
import HistoryEventsListItemAction from '@/modules/history/events/HistoryEventsListItemAction.vue';
import { HistoryEventAccountingRuleStatus, type HistoryEventEntry } from '@/modules/history/events/schemas';

/**
 * Seam: which unlink affordance the row offers. A missing accounting rule replaces the overflow
 * menu with the warning button, so an unlinkable row needs its own unlink button there or the
 * action is unreachable without expanding the subgroup.
 */
describe('modules/history/events/HistoryEventsListItemAction', () => {
  let wrapper: VueWrapper | undefined;

  afterEach(() => {
    wrapper?.unmount();
    wrapper = undefined;
  });

  function mountAction(options: { canUnlink?: boolean; missingRule?: boolean }): VueWrapper {
    const item = createMock<HistoryEventEntry>({
      eventAccountingRuleStatus: options.missingRule
        ? HistoryEventAccountingRuleStatus.NOT_PROCESSED
        : HistoryEventAccountingRuleStatus.HAS_RULE,
      identifier: 1,
    });

    wrapper = mount(HistoryEventsListItemAction, {
      global: { provide: libraryDefaults },
      props: { canUnlink: options.canUnlink, completeGroupEvents: [item], index: 0, item },
    });
    return wrapper;
  }

  it('should offer unlink as its own button when a missing rule hides the menu', async () => {
    const action = mountAction({ canUnlink: true, missingRule: true });

    expect(action.findComponent(HistoryEventAction).exists()).toBe(false);
    await action.find('[data-testid="row-unlink"]').trigger('click');
    expect(action.emitted('unlink-event')).toHaveLength(1);
  });

  it('should offer unlink through the menu when the rule is present', () => {
    const action = mountAction({ canUnlink: true });

    expect(action.find('[data-testid="row-unlink"]').exists()).toBe(false);
    expect(action.findComponent(HistoryEventAction).props('canUnlink')).toBe(true);
  });

  it('should offer no unlink at all for a row that cannot be unlinked', () => {
    const action = mountAction({ missingRule: true });

    expect(action.find('[data-testid="row-unlink"]').exists()).toBe(false);
  });
});

// Seam: what deleting an event asks its parent to do. The backend keeps the last event of an EVM or
// TRON transaction (rotkibv #11), so deleting the only event of one is offered as ignoring it.
function eventOf(entryType: HistoryEventEntryType, identifier: number): HistoryEventEntry {
  return createMock<HistoryEventEntry>({
    entryType,
    eventAccountingRuleStatus: HistoryEventAccountingRuleStatus.HAS_RULE,
    eventSubtype: 'none',
    identifier,
    location: 'tron',
  });
}

describe('modules/history/events/HistoryEventsListItemAction deletion', () => {
  let wrapper: VueWrapper<InstanceType<typeof HistoryEventsListItemAction>> | undefined;

  afterEach(() => {
    wrapper?.unmount();
    wrapper = undefined;
  });

  async function deleteEmitted(item: HistoryEventEntry, completeGroupEvents: HistoryEventEntry[]): Promise<unknown> {
    wrapper = mount(HistoryEventsListItemAction, {
      global: { stubs: { HistoryEventAction: true } },
      props: { completeGroupEvents, index: 0, item },
    });
    await wrapper.find('[data-testid=row-delete]').trigger('click');
    return wrapper.emitted('delete-event')?.at(-1);
  }

  it('should ignore the only event of a tron transaction instead of deleting it', async () => {
    const item = eventOf(HistoryEventEntryType.TRON_EVENT, 1);

    expect(await deleteEmitted(item, [item])).toEqual([{ event: item, type: 'ignore' }]);
  });

  it('should delete a tron event that is not the only one of its transaction', async () => {
    const item = eventOf(HistoryEventEntryType.TRON_EVENT, 1);

    expect(await deleteEmitted(item, [item, eventOf(HistoryEventEntryType.TRON_EVENT, 2)])).toEqual([{ ids: [1], type: 'delete' }]);
  });
});
