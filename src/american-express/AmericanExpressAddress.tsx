import React from 'react';
import { amexTheme } from '../lib/amexTheme';
import { AmexAppShell } from './AmexAppShell';
import { TypingAnimation } from '../components/TypingAnimation';
import { Dropdown } from '../components/Dropdown';
import { amexJob, amexAddress } from '../lib/amexData';

type Phase = 'idle' | 'open' | 'selected';

export const AmericanExpressAddress: React.FC<{
  countryPhase: Phase;
  addr1Revealed: number;
  cityRevealed: number;
  pinPhase: Phase;
  statePhase: Phase;
}> = ({ countryPhase, addr1Revealed, cityRevealed, pinPhase, statePhase }) => {
  return (
    <AmexAppShell jobTitle={amexJob.title} variant="plain">
      <div style={{ marginBottom: 26 }}>
        <div style={{ fontFamily: amexTheme.font, fontSize: 18, fontWeight: 700, color: amexTheme.colors.heading, marginBottom: 4 }}>
          Address
        </div>
        <div style={{ fontFamily: amexTheme.font, fontSize: 14, color: amexTheme.colors.muted }}>
          Please enter your home address.
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 26, maxWidth: 640 }}>
        <Dropdown label="Country" required value={amexAddress.country} options={['India', 'United States', 'United Kingdom']} phase={countryPhase} />
        <TypingAnimation label="Address Line 1" required value={amexAddress.addressLine1} revealed={addr1Revealed} />
        <TypingAnimation label="Address Line 2" value="" revealed={0} />
        <TypingAnimation label="Address Line 3" value="" revealed={0} />
        <TypingAnimation label="City or Town" required value={amexAddress.cityOrTown} revealed={cityRevealed} />
        <Dropdown label="Pin Code" required value={amexAddress.pinCode} options={[amexAddress.pinCode, '560002', '560025']} phase={pinPhase} />
        <Dropdown label="State" required value={amexAddress.state} options={[amexAddress.state, 'Maharashtra', 'Tamil Nadu']} phase={statePhase} />
      </div>
    </AmexAppShell>
  );
};
