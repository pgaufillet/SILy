/*
SILy
Copyright (C) 2024-2025 Pierre Gaufillet

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/

#ifndef MESHLINKTABLE_H
#define MESHLINKTABLE_H

#include <cstddef>
#include <cstdint>

struct MeshLink {
  uint16_t localAddr;
  uint16_t remoteAddr;
  int8_t snr;
  unsigned long lastRefreshTime; // Last refresh time in milliseconds
};

class MeshLinkTable {
public:
  MeshLinkTable(size_t capacity);
  ~MeshLinkTable();

  bool addOrUpdate(uint16_t local, uint16_t remote, int8_t snr);
  bool remove(uint16_t local, uint16_t remote);
  const MeshLink *find(uint16_t local, uint16_t remote) const;

  size_t count() const;
  const MeshLink *get(size_t index) const;

private:
  MeshLink *table_ = nullptr;
  size_t capacity_ = 0;
  size_t count_ = 0;
};

#endif // MESHLINKTABLE_H
